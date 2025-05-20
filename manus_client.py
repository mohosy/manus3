
import asyncio
import os
import json
import re
from typing import AsyncGenerator, Dict, List, Set
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from browserbase import Browserbase

import sqlite3
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dateparser.search import search_dates


load_dotenv()

# ── credentials / config ───────────────────────────────────────
MANUS_EMAIL        = "pccagent18@gmail.com"
MANUS_PASSWORD     = "thisisforpcc"
VERIFICATION_PHONE = "6263606593"

BB_API_KEY    = os.getenv("BROWSERBASE_API_KEY")
BB_PROJECT_ID = os.getenv("BROWSERBASE_PROJECT_ID")

if not BB_API_KEY or not BB_PROJECT_ID:
    raise EnvironmentError("Missing BROWSERBASE_API_KEY or BROWSERBASE_PROJECT_ID")

bb = Browserbase(api_key=BB_API_KEY)

# ── sentinel tokens ────────────────────────────────────────────
END_TOKEN   = "END"   # must appear in Manus output when done
ERROR_TOKEN = "ERROR" # triggers fail‑fast path

PROMPT_TAIL = (
    "\n\nRespond in plain text only.\n"
    f"When your final answer is complete, write {END_TOKEN} on a new line.\n"
    f"If you cannot answer, write {ERROR_TOKEN} on a new line.\n"
    "Do not ask follow‑up questions. Do not wrap anything in docs or code blocks.\n"
)

TIMEOUT_LOOPS    = 120   # 120 × 2s = 4 min max
POLL_INTERVAL_MS = 2000

# ── main public class ──────────────────────────────────────────

# ── scheduler setup ────────────────────────────────────────────
DB_PATH = "scheduled_jobs.db"
PST     = "America/Los_Angeles"

sched = AsyncIOScheduler(timezone=PST)
sched.start()

def _init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS scheduled_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fire_at   TEXT NOT NULL,
                prompt    TEXT NOT NULL,
                status    TEXT NOT NULL DEFAULT 'pending',
                answer    TEXT,
                session_id TEXT NOT NULL
        )""")
# -------- scheduling wrapper ---------------------------------
def ask_or_schedule(self, nl_command: str, session_id: str | None = None):
    """If text includes a future time, schedule; else run immediately."""
    dt_fire, pure_prompt = self._extract_schedule(nl_command)
    if dt_fire and dt_fire > datetime.now():
        if session_id is None:
            session_id = "anon"
        job_id = self._schedule_job(pure_prompt, dt_fire, session_id)
        return {
            "logs": [],
            "answer": f"🗓️ locked in for {dt_fire:%a %b %d @ %I:%M%p}. job #{job_id}"
        }
    # immediate
    return self.ask_manus(nl_command)

# -------- helper: parse natural language date ----------------
@staticmethod
def _extract_schedule(nl_text: str):
    results = search_dates(
        nl_text,
        settings={
            "TIMEZONE": PST,
            "RETURN_AS_TIMEZONE_AWARE": False,
            "PREFER_DATES_FROM": "future",
        },
    )
    if not results:
        return None, nl_text
    for phrase, dt_obj in results:
        if dt_obj > datetime.now():
            stripped = nl_text.replace(phrase, "").strip(", ").lstrip()
            return dt_obj, stripped
    return None, nl_text

# -------- helper: save + APScheduler -------------------------
def _schedule_job(self, prompt: str, fire_at: datetime, session_id: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    cur.execute(
        "INSERT INTO scheduled_jobs(fire_at,prompt,status,session_id) VALUES (?,?,?,?)",
        (fire_at.isoformat(), prompt, "pending", session_id)
    )
    job_id = cur.lastrowid
    conn.commit()
    conn.close()

    sched.add_job(ManusClient._run_manus_job_static,
                  trigger="date",
                  id=str(job_id),
                  next_run_time=fire_at,
                  args=[prompt, job_id, session_id])
    return job_id

# -------- background job runner ------------------------------
@staticmethod
async def _run_manus_job_static(prompt: str, job_id: int, session_id: str):
    cli = ManusClient()
    res = cli.ask_manus(prompt)
    answer_json = json.dumps(res, ensure_ascii=False)
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    cur.execute(
        "UPDATE scheduled_jobs SET status='done', answer=? WHERE id=?",
        (answer_json, job_id)
    )
    conn.commit()
    conn.close()
    # TODO: emit to websocket room f"session:{session_id}"
    """
    Cloud‑browser wrapper around Manus.AI.
    ask_manus(prompt)  -> {'logs': [...], 'answer': '...'}
    stream_manus(...)  -> async generator chunks (log / answer)
    """

    # -------- public (batch) --------
    def ask_manus(self, prompt: str) -> Dict[str, List[str]]:
        logs: List[str] = []

        def log(line: str):
            print(line)
            logs.append(line)

        answer = asyncio.run(self._interact_with_manus(prompt, log))
        return {"logs": logs, "answer": answer}

    # -------- public (stream) -------
    async def stream_manus(self, prompt: str) -> AsyncGenerator[Dict[str, str], None]:
        async for chunk in self._stream_interact_with_manus(prompt):
            yield chunk

    # -------- internal (batch) ------
    async def _interact_with_manus(self, prompt: str, log) -> str:
        prompt = (
            "Context: You are answering on behalf of Pasadena City College (PCC). "
            "Most questions relate to PCC programs, admissions, resources, student life, etc. "
            "Provide PCC‑specific information whenever relevant.\n\n"
            + prompt
            + PROMPT_TAIL
        )
        log("🚀 spinning up remote chromium session on Browserbase…")

        session = bb.sessions.create(project_id=BB_PROJECT_ID)
        log(f"🔗 connected. live view: https://browserbase.com/sessions/{session.id}")

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(session.connect_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page    = context.pages[0]   if context.pages   else await context.new_page()

            # login handling
            if not os.path.exists("state.json"):
                await self._google_login(page, context, log)
            else:
                with open("state.json", "r", encoding="utf-8") as f:
                    cookies = json.load(f).get("cookies", [])
                    if cookies:
                        await context.add_cookies(cookies)

            await self._manus_login(page, log)
            answer = await self._send_prompt(page, prompt, log)
            await browser.close()
            log("✅ remote browser closed.")
            return answer

    # ------- internal (stream) ------

async def _stream_interact_with_manus(self, prompt: str) -> AsyncGenerator[Dict[str, str], None]:
    prompt = (
        "Context: You are answering on behalf of Pasadena City College (PCC). "
        "Most questions relate to PCC programs, admissions, resources, student life, etc. "
        "Provide PCC‑specific information whenever relevant.\n\n"
        + prompt
        + PROMPT_TAIL
    )
    yield {"type": "log", "message": "🚀 spinning up remote chromium session on Browserbase…"}

    session = bb.sessions.create(project_id=BB_PROJECT_ID)
    sid = session.id
    try:
        yield {"type": "log", "message": f"🔗 connected. live view: https://browserbase.com/sessions/{session.id}"}

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(session.connect_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page    = context.pages[0]   if context.pages   else await context.new_page()

            # login flow
            if not os.path.exists("state.json"):
                async for l in self._google_login_stream(page, context):
                    yield l
            else:
                with open("state.json", "r", encoding="utf-8") as f:
                    cookies = json.load(f).get("cookies", [])
                    if cookies:
                        await context.add_cookies(cookies)
                        yield {"type": "log", "message": "🔓 cookies loaded from state.json."}

            async for l in self._manus_login_stream(page):
                yield l

            # prompt / answer
            async for chunk in self._send_prompt_stream(page, prompt):
                yield chunk

            await browser.close()
            yield {"type": "log", "message": "✅ remote browser closed."}
    finally:
        try:
            bb.sessions.delete(project_id=BB_PROJECT_ID, session_id=sid)
        except Exception:
            pass
    async def _google_login(self, page, context, log):
        log("🔐 performing one‑time Google login…")
        await page.goto("https://accounts.google.com/signin/v2/identifier?service=mail")
        await page.fill('input[type="email"]', MANUS_EMAIL)
        await page.click('button:has-text("Next")')
        await page.wait_for_selector('input[type="password"]', timeout=10000)
        await page.fill('input[type="password"]', MANUS_PASSWORD)
        await page.click('button:has-text("Next")')
        await page.wait_for_timeout(5000)
        if await page.locator('input[type="tel"]').is_visible(timeout=5000):
            await page.fill('input[type="tel"]', VERIFICATION_PHONE)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(5000)
        await context.storage_state(path="state.json")
        log("🔒 google auth completed & cookies saved.")

    async def _google_login_stream(self, page, context):
        yield {"type": "log", "message": "🔐 performing one‑time Google login…"}
        await page.goto("https://accounts.google.com/signin/v2/identifier?service=mail")
        await page.fill('input[type="email"]', MANUS_EMAIL)
        await page.click('button:has-text("Next")')
        await page.wait_for_selector('input[type="password"]', timeout=10000)
        await page.fill('input[type="password"]', MANUS_PASSWORD)
        await page.click('button:has-text("Next")')
        await page.wait_for_timeout(5000)
        if await page.locator('input[type="tel"]').is_visible(timeout=5000):
            await page.fill('input[type="tel"]', VERIFICATION_PHONE)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(5000)
        await context.storage_state(path="state.json")
        yield {"type": "log", "message": "🔒 google auth completed & cookies saved."}

    async def _manus_login(self, page, log):
        log("📄 navigating to Manus login…")
        await page.goto("https://manus.im/login")
        try:
            btn = page.locator("text=Sign up with Google")
            if await btn.is_visible(timeout=5000):
                await btn.click()
                await page.wait_for_url("**/app", timeout=15000)
                await page.wait_for_timeout(3000)
                log("✅ Manus dashboard loaded.")
        except Exception as e:
            log(f"⚠️ manus login issue: {e}")

    async def _manus_login_stream(self, page):
        yield {"type": "log", "message": "📄 navigating to Manus login…"}
        await page.goto("https://manus.im/login")
        try:
            btn = page.locator("text=Sign up with Google")
            if await btn.is_visible(timeout=5000):
                await btn.click()
                await page.wait_for_url("**/app", timeout=15000)
                await page.wait_for_timeout(3000)
                yield {"type": "log", "message": "✅ Manus dashboard loaded."}
        except Exception as e:
            yield {"type": "log", "message": f"⚠️ manus login issue: {e}"}

    # -------- prompt helpers --------
    async def _send_prompt(self, page, prompt, log) -> str:
        log(f"🧠 sending prompt → {prompt[:60]}…")
        await page.fill("textarea", prompt)
        await page.keyboard.press("Enter")

        log("📡 Lance O' Lot is working… (waiting for END/ERROR token) 🐴")
        seen: Set[str] = set()

        for _ in range(TIMEOUT_LOOPS):
            await page.wait_for_timeout(POLL_INTERVAL_MS)
            for block in await page.query_selector_all("div[data-message-id], div.prose"):
                try:
                    text = (await block.inner_text()).strip()
                    if text and text not in seen:
                        seen.add(text)
                        log(f"💬 {text[:80]}")
                        if self._has_error(text):
                            return "[❌] Manus signaled ERROR."
                        if self._has_end(text):
                            return self._strip_end_token(text)
                except Exception:
                    continue
        return "[❌] Manus response timed out without END or ERROR."

    async def _send_prompt_stream(self, page, prompt):
        yield {"type": "log", "message": f"🧠 sending prompt → {prompt[:60]}…"}
        await page.fill("textarea", prompt)
        await page.keyboard.press("Enter")

        yield {"type": "log", "message": "📡 Lance O' Lot is working… (waiting for END/ERROR token) 🐴"}
        seen: Set[str] = set()

        for _ in range(TIMEOUT_LOOPS):
            await page.wait_for_timeout(POLL_INTERVAL_MS)
            for block in await page.query_selector_all("div[data-message-id], div.prose"):
                try:
                    text = (await block.inner_text()).strip()
                    if text and text not in seen:
                        seen.add(text)
                        yield {"type": "log", "message": f"💬 {text[:80]}"}
                        if self._has_error(text):
                            yield {"type": "answer", "message": "[❌] Manus signaled ERROR."}
                            return
                        if self._has_end(text):
                            yield {"type": "answer", "message": self._strip_end_token(text)}
                            return
                except Exception:
                    continue
        yield {"type": "answer", "message": "[❌] Manus response timed out without END or ERROR."}

    # -------- token utils ----------
    @staticmethod
    def _has_end(text: str) -> bool:
        return bool(re.search(r"(^|\s)END(\s|[.!?]|$)", text))

    @staticmethod
    def _has_error(text: str) -> bool:
        return bool(re.search(r"(^|\s)ERROR(\s|[.!?]|$)", text))

    @staticmethod
    def _strip_end_token(text: str) -> str:
        return re.sub(r"(^|\s)END(\s|[.!?]|$)", "", text).rstrip()

def _load_pending_jobs():
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    now_iso = datetime.now().isoformat()
    rows = cur.execute(
        "SELECT id, fire_at, prompt, session_id FROM scheduled_jobs WHERE status='pending' AND fire_at > ?",
        (now_iso,)
    ).fetchall()
    conn.close()
    for job_id, fire_at, prompt, session_id in rows:
        when = datetime.fromisoformat(fire_at)
        sched.add_job(ManusClient._run_manus_job_static,
                      trigger="date",
                      id=str(job_id),
                      next_run_time=when,
                      args=[prompt, job_id, session_id])

# initialize
_init_db()
_load_pending_jobs()

# ─────────── monkey‑patch: restore missing class + global wrappers ────────────
class ManusClient:
    """Thin shell so external code can still do `ManusClient().ask_manus(...)`
    even though the original refactor flattened the methods.
    """

    # batch
    def ask_manus(self, prompt: str):
        return ask_manus(self, prompt)

    # streaming
    async def stream_manus(self, prompt: str):
        async for chunk in _stream_manus_impl(self, prompt):
            yield chunk

    # natural‑language scheduler helper
    def ask_or_schedule(self, nl_command: str, session_id: str | None = None):
        return ask_or_schedule(self, nl_command, session_id)

    # expose static run helper for APScheduler
    _run_manus_job_static = staticmethod(_run_manus_job_static)


# keep the old top‑level API for code that imports it directly
async def _stream_manus_impl(dummy_self, prompt: str):
    """Internal implementation that matches the signature of the old method."""
    async for chunk in _stream_interact_with_manus(dummy_self, prompt):
        yield chunk


async def stream_manus(prompt: str):
    """Module‑level convenience wrapper so `from manus_client import stream_manus`
    keeps working. Returns the same async generator the class method yields.
    """
    cli = ManusClient()
    async for chunk in cli.stream_manus(prompt):
        yield chunk


def ask_manus_batch(prompt: str):
    """Alias for backward compatibility."""
    return ManusClient().ask_manus(prompt)
