
import asyncio
import os
import json
from typing import AsyncGenerator, Dict, Optional
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, Browser
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

SYSTEM_CONTEXT = (
    "You are Manus AI, answering questions **mostly about Pasadena City College**. "
    "Unless the prompt is clearly unrelated, assume it’s PCC-specific and answer directly. "
    "Do **NOT** ask the user follow-up questions or request clarification. "
)

# ── in‑memory session cache ────────────────────────────────────
# one PCC chat window  ===> one live Manus tab
_session_cache: Dict[str, "_Session"] = {}


class _Session:
    """Light wrapper around a Browserbase session + single persistent Manus tab."""

    def __init__(self, connect_url: str, browser: Browser, page: Page):
        self.connect_url = connect_url
        self.browser = browser
        self.page = page

    async def close(self):
        try:
            await self.browser.close()
        except Exception:
            pass


# ── public client class ────────────────────────────────────────
class ManusClient:
    """
    Cloud‑browser wrapper around Manus.ai that **remembers** a conversation
    for each front‑end chatSessionId (sticky Manus tab).

    Public:
        • ask_manus(prompt, chat_id="default")  -> {logs, answer}
        • stream_manus(prompt, chat_id="default") -> async generator of chunks
        • stop_session(chat_id)                   -> None  (front‑end Stop btn)
    """

    # ------------- public (batch) -------------
    def ask_manus(self, prompt: str, chat_id: str = "default") -> Dict[str, list]:
        logs: list[str] = []

        def log(line: str):
            print(line)
            logs.append(line)

        answer = asyncio.run(self._interact_with_manus(prompt, chat_id, log))
        return {"logs": logs, "answer": answer}

    # ------------- public (stream) -------------
    async def stream_manus(
        self, prompt: str, chat_id: str = "default"
    ) -> AsyncGenerator[Dict[str, str], None]:
        async for chunk in self._stream_interact_with_manus(prompt, chat_id):
            yield chunk

    # ------------- public (close) -------------
    async def stop_session(self, chat_id: str = "default"):
        sess = _session_cache.pop(chat_id, None)
        if sess:
            await sess.close()

    # ------------- internal (batch path) ------------
    async def _interact_with_manus(self, prompt: str, chat_id: str, log) -> str:
        prompt = SYSTEM_CONTEXT + prompt + " (say END when you're done writing your final answer)"

        page, live_view = await self._get_or_create_page(chat_id, log)

        log(f"🧠 sending prompt → {prompt[:60]}…")
        await page.fill("textarea", prompt)
        await page.keyboard.press("Enter")

        log("📡 waiting for END token…")
        seen = set()
        for _ in range(60):  # 2‑minute timeout
            await page.wait_for_timeout(2000)
            for block in await page.query_selector_all("div[data-message-id], div.prose"):
                try:
                    text = await block.inner_text()
                    if text not in seen:
                        seen.add(text)
                        log(f"💬 {text.strip()}")
                        if "END" in text:
                            return text.replace("END", "").strip()
                except Exception:
                    continue
        return "[❌] Manus response did not include END in time."

    # ------------- internal (stream path) ------------
    async def _stream_interact_with_manus(
        self, prompt: str, chat_id: str
    ) -> AsyncGenerator[Dict[str, str], None]:
        prompt = SYSTEM_CONTEXT + prompt + " (say END when you're done writing your final answer)"
        page, live_view = await self._get_or_create_page(chat_id)
        yield {"type": "log", "message": f"🔗 connected. live view: {live_view}"}

        yield {"type": "log", "message": f"🧠 sending prompt → {prompt[:60]}…"}
        await page.fill("textarea", prompt)
        await page.keyboard.press("Enter")

        yield {"type": "log", "message": "📡 waiting for END token…"}
        seen = set()

        async def new_texts():
            for block in await page.query_selector_all("div[data-message-id], div.prose"):
                try:
                    text = await block.inner_text()
                    if text not in seen:
                        seen.add(text)
                        yield text.strip()
                except Exception:
                    continue

        for _ in range(60):
            await page.wait_for_timeout(2000)
            async for txt in new_texts():
                yield {"type": "log", "message": f"💬 {txt}"}
                if "END" in txt:
                    clean = txt.replace("END", "").strip()
                    yield {"type": "answer", "message": clean}
                    return
        yield {"type": "answer", "message": "[❌] Manus response did not include END in time."}

    # ------------- helpers -------------
    async def _get_or_create_page(self, chat_id: str, log=lambda *_: None):
        """Return sticky playwright Page for chat_id, creating if needed."""
        # already active?
        if chat_id in _session_cache:
            sess = _session_cache[chat_id]
            return sess.page, f"https://browserbase.com/sessions/{sess.browser.contexts[0].browser.session.id if hasattr(sess.browser, 'session') else 'n/a'}"

        # new session
        log("🚀 spinning up remote chromium session on Browserbase…")
        session = bb.sessions.create(project_id=BB_PROJECT_ID)
        live_view = f"https://browserbase.com/sessions/{session.id}"
        log(f"🔗 connected. live view: {live_view}")

        async_playwright_obj = await async_playwright().start()
        browser = await async_playwright_obj.chromium.connect_over_cdp(session.connect_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page: Page = context.pages[0] if context.pages else await context.new_page()

        # try loading cookies
        if os.path.exists("state.json"):
            with open("state.json", "r", encoding="utf-8") as f:
                cookies = json.load(f).get("cookies", [])
                if cookies:
                    await context.add_cookies(cookies)
                    log("🔓 cookies loaded from state.json.")

        # Google & Manus login (only once per new session)
        if not await self._is_logged_into_manus(page):
            await self._google_login(page, context, log)
            await self._manus_login(page, log)

        # cache & return
        _session_cache[chat_id] = _Session(session.connect_url, browser, page)
        return page, live_view

    async def _is_logged_into_manus(self, page: Page) -> bool:
        try:
            await page.goto("https://manus.im/app", timeout=10000)
            await page.wait_for_selector("textarea", timeout=10000)
            return True
        except Exception:
            return False

    # ---------- login helpers (unchanged except for 'self' access) ----------
    async def _google_login(self, page, context, log):
        log("🔐 performing one-time Google login…")
        await page.goto("https://accounts.google.com/signin/v2/identifier?service=mail")
        await page.fill('input[type="email"]', MANUS_EMAIL)
        await page.click('button:has-text("Next")')
        await page.wait_for_selector('input[type="password"], input[name="Passwd"], input[autocomplete="current-password"]', timeout=15000)
        await page.fill('input[name="Passwd"]', MANUS_PASSWORD) if await page.locator('input[name="Passwd"]').is_visible(timeout=1000) else await page.fill('input[type="password"]', MANUS_PASSWORD)
        await page.click('button:has-text("Next")')
        await page.wait_for_timeout(5000)

        if await page.locator('input[type="tel"]').is_visible(timeout=5000):
            await page.fill('input[type="tel"]', VERIFICATION_PHONE)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(5000)

        await context.storage_state(path="state.json")
        log("🔒 google auth completed & cookies saved.")

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
