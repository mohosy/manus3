
import asyncio
import os
import json
from typing import AsyncGenerator, Dict, List, Set
from dotenv import load_dotenv
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
END_TOKEN   = "END"   # must appear ON ITS OWN LINE when Manus is done
ERROR_TOKEN = "ERROR" # triggers fail‑fast path

PROMPT_TAIL = (
    "\n\nRespond in plain text only.\n"
    f"When your final answer is complete, write {END_TOKEN} on a new line.\n"
    f"If you cannot answer, write {ERROR_TOKEN} on a new line.\n"
    "Do not ask follow‑up questions. Do not wrap anything in docs or code blocks.\n"
)

TIMEOUT_LOOPS   = 90   # 90 × 2 s  = 180 s max wait
POLL_INTERVAL_MS = 2000

# ── main public class ──────────────────────────────────────────
class ManusClient:
    """
    Cloud‑browser wrapper around Manus.AI.

    • ask_manus(prompt)        -> {"logs": [...], "answer": "..."}
      (returns everything at once, back‑compat.)

    • stream_manus(prompt) -> async generator yielding:
        {"type": "log",    "message": "..."}      # for each thinking blurb
        {"type": "answer", "message": "final"}    # final answer, END removed
    """

    # ------------- public (batch) -------------
    def ask_manus(self, prompt: str) -> Dict[str, list]:
        logs: List[str] = []

        # helper that prints AND stores
        def log(line: str):
            print(line)
            logs.append(line)

        answer = asyncio.run(self._interact_with_manus(prompt, log))
        return {"logs": logs, "answer": answer}

    # ------------- public (stream) -------------
    async def stream_manus(self, prompt: str) -> AsyncGenerator[Dict[str, str], None]:
        async for chunk in self._stream_interact_with_manus(prompt):
            yield chunk

    # ------------- internal (batch path) ------------
    async def _interact_with_manus(self, prompt: str, log) -> str:
        prompt += PROMPT_TAIL
        log("🚀 spinning up remote chromium session on Browserbase…")

        session = bb.sessions.create(project_id=BB_PROJECT_ID)
        log(f"🔗 connected. live view: https://browserbase.com/sessions/{session.id}")

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(session.connect_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page    = context.pages[0]   if context.pages   else await context.new_page()

            # ── login flow ──
            if not os.path.exists("state.json" ):
                await self._google_login(page, context, log)
            else:
                with open("state.json", "r", encoding="utf-8") as f:
                    cookies = json.load(f).get("cookies", [])
                    if cookies:
                        await context.add_cookies(cookies)

            await self._manus_login(page, log)

            # ── prompt / answer ──
            answer = await self._send_prompt(page, prompt, log)

            await browser.close()
            log("✅ remote browser closed.")
            return answer

    # ------------- internal (stream path) ------------
    async def _stream_interact_with_manus(self, prompt: str) -> AsyncGenerator[Dict[str, str], None]:
        prompt += PROMPT_TAIL
        yield {"type": "log", "message": "🚀 spinning up remote chromium session on Browserbase…"}

        session = bb.sessions.create(project_id=BB_PROJECT_ID)
        yield {"type": "log", "message": f"🔗 connected. live view: https://browserbase.com/sessions/{session.id}"}

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(session.connect_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page    = context.pages[0]   if context.pages   else await context.new_page()

            # ── login flow ──
            if not os.path.exists("state.json" ):
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

            # ── prompt / answer ──
            async for chunk in self._send_prompt_stream(page, prompt):
                yield chunk

            await browser.close()
            yield {"type": "log", "message": "✅ remote browser closed."}

    # ------------- helpers -------------
    async def _google_login(self, page, context, log):
        log("🔐 performing one‑time Google login…")
        await page.goto("https://accounts.google.com/signin/v2/identifier?service=mail")
        await page.fill('input[type="email"]', MANUS_EMAIL)
        await page.click('button:has-text("Next")')
        await page.wait_for_selector('input[type="password"]', timeout=10000)
        await page.fill('input[type="password"]', MANUS_PASSWORD)
        await page.click('button:has-text("Next")')
        await page.wait_for_timeout(5000)

        # potential phone verification
        if await page.locator('input[type="tel"]').is_visible(timeout=5000):
            await page.fill('input[type="tel"]', VERIFICATION_PHONE)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(5000)

        await context.storage_state(path="state.json")
        log("🔒 google auth completed & cookies saved.")

    async def _google_login_stream(self, page, context) -> AsyncGenerator[Dict[str, str], None]:
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

    async def _manus_login_stream(self, page) -> AsyncGenerator[Dict[str, str], None]:
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

    # ── prompt send / polling helpers ───────────────────────────
    async def _send_prompt(self, page, prompt, log) -> str:
        log(f"🧠 sending prompt → {prompt[:60]}…")
        await page.fill("textarea", prompt)
        await page.keyboard.press("Enter")

        log("📡 waiting for END/ERROR token…")
        seen: Set[str] = set()

        for _ in range(TIMEOUT_LOOPS):
            await page.wait_for_timeout(POLL_INTERVAL_MS)
            for block in await page.query_selector_all("div[data-message-id], div.prose"):
                try:
                    text = (await block.inner_text()).strip()
                    if text and text not in seen:
                        seen.add(text)
                        log(f"💬 {text[:80]}")

                        # sentinel checks (token must be standalone line or end of text)
                        if self._has_error(text):
                            return "[❌] Manus signaled ERROR."
                        if self._has_end(text):
                            clean = self._strip_end_token(text)
                            return clean
                except Exception:
                    continue

        return "[❌] Manus response timed out without END or ERROR."

    async def _send_prompt_stream(self, page, prompt) -> AsyncGenerator[Dict[str, str], None]:
        yield {"type": "log", "message": f"🧠 sending prompt → {prompt[:60]}…"}
        await page.fill("textarea", prompt)
        await page.keyboard.press("Enter")

        yield {"type": "log", "message": "📡 waiting for END/ERROR token…"}
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
                            clean = self._strip_end_token(text)
                            yield {"type": "answer", "message": clean}
                            return
                except Exception:
                    continue

        yield {"type": "answer", "message": "[❌] Manus response timed out without END or ERROR."}

    # ── utility helpers ────────────────────────────────────────
    @staticmethod
    def _has_end(text: str) -> bool:
        # END on its own line OR at very end after newline
        return bool(re.search(r"(^|\n)END(\n|$)", text))

    @staticmethod
    def _has_error(text: str) -> bool:
        return bool(re.search(r"(^|\n)ERROR(\n|$)", text))

    @staticmethod
    def _strip_end_token(text: str) -> str:
        return re.sub(r"(^|\n)END(\n|$)", "", text).strip()
