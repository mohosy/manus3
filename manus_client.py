import asyncio
import os
import json
import re
import sqlite3
from datetime import datetime
from typing import AsyncGenerator, Dict, List, Set, Tuple, Optional

from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dateparser.search import search_dates
from playwright.async_api import async_playwright
from browserbase import Browserbase

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
                fire_at TEXT NOT NULL,
                prompt  TEXT NOT NULL,
                status  TEXT NOT NULL DEFAULT 'pending',
                answer  TEXT
            )""")
    conn.commit()
    conn.close()

def _load_pending_jobs() -> None:
    """Re‑hydrate jobs after a restart."""
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    now_iso = datetime.now().isoformat()
    rows = cur.execute(
        "SELECT id, fire_at, prompt FROM scheduled_jobs WHERE status='pending' AND fire_at > ?",
        (now_iso,)
    ).fetchall()
    conn.close()

    for job_id, fire_at, prompt in rows:
        when = datetime.fromisoformat(fire_at)
        sched.add_job(ManusClient._run_manus_job_static,
                      trigger="date",
                      id=str(job_id),
                      next_run_time=when,
                      args=[prompt, job_id])

_init_db()  # run on import

# ── main public class ──────────────────────────────────────────
class ManusClient:
    """
    Cloud‑browser wrapper around Manus.AI.
    ask_manus(prompt)          -> {'logs': [...], 'answer': '...'}
    stream_manus(prompt)       -> async generator chunks (log / answer)
    ask_or_schedule(nl_text)   -> {...} executes immediately or schedules
    """

    # -------- public (batch) --------
    def ask_manus(self, prompt: str) -> Dict[str, List[str]]:
        logs: List[str] = []

        def log(line: str):
            print(line)
            logs.append(line)

        answer = asyncio.run(self._interact_with_manus(prompt, log))
        return {"logs": logs, "answer": answer}

    # -------- scheduling wrapper -----
    def ask_or_schedule(self, nl_command: str) -> Dict[str, List[str] | str]:
        """Detect natural‑language time phrases; schedule or run immediately."""
        dt_fire, pure_prompt = self._extract_schedule(nl_command)
        if dt_fire and dt_fire > datetime.now():
            job_id = self._schedule_job(pure_prompt, dt_fire)
            return {
                "logs": [],
                "answer": f"🗓️ locked in for {dt_fire:%a %b %d @ %I:%M%p}. job #{job_id}"
            }
        # else normal immediate run
        return self.ask_manus(nl_command)

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

    # ── scheduling helpers ─────────────────────────────────────
    @staticmethod
    def _extract_schedule(nl: str) -> Tuple[Optional[datetime], str]:
        """Return (datetime, stripped_prompt) if future timestamp found."""
        results = search_dates(
            nl,
            settings={
                "TIMEZONE": PST,
                "RETURN_AS_TIMEZONE_AWARE": False,
                "PREFER_DATES_FROM": "future",
            },
        )
        if not results:
            return None, nl
        # pick first future date
        for phrase, dt_obj in results:
            if dt_obj > datetime.now():
                stripped = nl.replace(phrase, "").strip(", ").lstrip()
                return dt_obj, stripped
        # none in future
        return None, nl

    def _schedule_job(self, prompt: str, fire_at: datetime) -> int:
        conn = sqlite3.connect(DB_PATH)
        cur  = conn.cursor()
        cur.execute(
            "INSERT INTO scheduled_jobs(fire_at, prompt, status) VALUES (?,?,?)",
            (fire_at.isoformat(), prompt, "pending")
        )
        job_id = cur.lastrowid
        conn.commit()
        conn.close()

        sched.add_job(ManusClient._run_manus_job_static,
                      trigger="date",
                      id=str(job_id),
                      next_run_time=fire_at,
                      args=[prompt, job_id])
        return job_id

    @staticmethod
    async def _run_manus_job_static(prompt: str, job_id: int) -> None:
        """Standalone task run: creates a fresh ManusClient, executes, stores answer."""
        cli   = ManusClient()
        res   = cli.ask_manus(prompt)  # blocking inside
        answer_json = json.dumps(res, ensure_ascii=False)

        conn = sqlite3.connect(DB_PATH)
        cur  = conn.cursor()
        cur.execute(
            "UPDATE scheduled_jobs SET status='done', answer=? WHERE id=?",
            (answer_json, job_id)
        )
        conn.commit()
        conn.close()
        # TODO: push the answer back to your bot / chat via websocket or webhook

    # ── original helper methods unchanged below ─────────────────
