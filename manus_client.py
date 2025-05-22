
import asyncio
import os
import json
from typing import AsyncGenerator, Dict, Optional
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, Browser
from browserbase import Browserbase

load_dotenv()

# ── credentials / config ───────────────────────────────────────
MANUS_EMAIL = "pccagent18@gmail.com"
MANUS_PASSWORD = "thisisforpcc"
VERIFICATION_PHONE = "6263606593"

BB_API_KEY = os.getenv("BROWSERBASE_API_KEY")
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
_session_cache: Dict[str, 'ManusSession'] = {}


class ManusSession:
    """Keeps a persistent Browserbase page tied to a chat_id."""

    def __init__(self, session_id: str, browser: Browser, page: Page):
        self.session_id = session_id
        self.browser = browser
        self.page = page

    async def close(self):
        try:
            await self.browser.close()
        except Exception:
            pass


class ManusClient:
    """High‑level API used by your backend routes."""

    # ------------------- public sync -------------------
    def ask_manus(self, prompt: str, chat_id: str = "default") -> Dict[str, list]:
        logs: list[str] = []

        def log(line: str):
            print(line)
            logs.append(line)

        answer = asyncio.run(self._interact_with_manus(prompt, chat_id, log))
        return {"logs": logs, "answer": answer}

    # ------------------- public stream -----------------
    async def stream_manus(
        self, prompt: str, chat_id: str = "default"
    ) -> AsyncGenerator[Dict[str, str], None]:
        async for chunk in self._stream_interact_with_manus(prompt, chat_id):
            yield chunk

    # ------------------- public stop -------------------
    async def stop_session(self, chat_id: str = "default"):
        sess = _session_cache.pop(chat_id, None)
        if sess:
            await sess.close()

    # ===================================================
    # INTERNAL BELOW
    # ===================================================

    async def _interact_with_manus(self, prompt: str, chat_id: str, log) -> str:
        prompt = (
            SYSTEM_CONTEXT
            + prompt
            + " (say END when you're done writing your final answer)"
        )
        page, _ = await self._get_or_create_page(chat_id, log)

        log(f"🧠 sending prompt → {prompt[:60]}…")
        input_box = await self._get_input_box(page)
        await input_box.fill(prompt)
        await page.keyboard.press("Enter")

        log("📡 waiting for END token…")
        seen = set()
        for _ in range(60):  # up to 2 min
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

    async def _stream_interact_with_manus(
        self, prompt: str, chat_id: str
    ) -> AsyncGenerator[Dict[str, str], None]:
        prompt = (
            SYSTEM_CONTEXT
            + prompt
            + " (say END when you're done writing your final answer)"
        )
        page, live_view = await self._get_or_create_page(chat_id, lambda *_: None)
        yield {"type": "log", "message": f"🔗 connected. live view: {live_view}"}

        yield {"type": "log", "message": f"🧠 sending prompt → {prompt[:60]}…"}
        input_box = await self._get_input_box(page)
        await input_box.fill(prompt)
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

    # ------------------- helpers -------------------
    async def _get_or_create_page(self, chat_id: str, log):
        # reuse if exists
        if chat_id in _session_cache:
            sess = _session_cache[chat_id]
            return sess.page, f"https://browserbase.com/sessions/{sess.session_id}"

        # else spin up new
        log("🚀 spinning up remote chromium session on Browserbase…")
        session = bb.sessions.create(project_id=BB_PROJECT_ID)
        live_view = f"https://browserbase.com/sessions/{session.id}"
        log(f"🔗 connected. live view: {live_view}")

        p = await async_playwright().start()
        browser = await p.chromium.connect_over_cdp(session.connect_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()

        # load cookies if they exist
        if os.path.exists("state.json"):
            with open("state.json", "r", encoding="utf-8") as f:
                cookies = json.load(f).get("cookies", [])
                if cookies:
                    await context.add_cookies(cookies)
                    log("🔓 cookies loaded from state.json.")

        # login if necessary
        if not await self._is_logged_into_manus(page):
            await self._google_login(page, context, log)
            await self._manus_login(page, log)

        # store
        _session_cache[chat_id] = ManusSession(session.id, browser, page)
        return page, live_view

    async def _get_input_box(self, page: Page):
        """Return visible & enabled compose box, scrolling into view if hidden."""
        timeout_ms = 10000
        interval = 500
        elapsed = 0
        while elapsed < timeout_ms:
            for sel in ["textarea", "[contenteditable='true']"]:
                for handle in await page.query_selector_all(sel):
                    try:
                        if await handle.is_enabled():
                            await handle.scroll_into_view_if_needed()
                            if await handle.is_visible():
                                return handle
                    except Exception:
                        continue
            await page.wait_for_timeout(interval)
            elapsed += interval
        raise RuntimeError("Could not find visible Manus compose box.")

    async def _is_logged_into_manus(self, page: Page) -> bool:
        try:
            await page.goto("https://manus.im/app", timeout=10000)
            await page.wait_for_selector("textarea, [contenteditable='true']", timeout=10000)
            return True
        except Exception:
            return False

    # ------------ login helpers ------------
    async def _google_login(self, page: Page, context, log):
        log("🔐 performing one-time Google login…")
        await page.goto(
            "https://accounts.google.com/signin/v2/identifier?service=mail", timeout=30000
        )
        await page.fill('input[type="email"]', MANUS_EMAIL)
        await page.click('button:has-text("Next")')
        await page.wait_for_selector(
            'input[type="password"], input[name="Passwd"], input[autocomplete="current-password"]',
            timeout=15000,
        )
        if await page.locator('input[name="Passwd"]').is_visible(timeout=1000):
            await page.fill('input[name="Passwd"]', MANUS_PASSWORD)
        else:
            await page.fill('input[type="password"]', MANUS_PASSWORD)
        await page.click('button:has-text("Next")')
        await page.wait_for_timeout(5000)

        if await page.locator('input[type="tel"]').is_visible(timeout=5000):
            await page.fill('input[type="tel"]', VERIFICATION_PHONE)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(5000)

        await context.storage_state(path="state.json")
        log("🔒 google auth completed & cookies saved.")

    async def _manus_login(self, page: Page, log):
        log("📄 navigating to Manus login…")
        await page.goto("https://manus.im/login", timeout=30000)
        try:
            btn = page.locator("text=Sign up with Google")
            if await btn.is_visible(timeout=5000):
                await btn.click()
                await page.wait_for_url("**/app", timeout=15000)
                await page.wait_for_timeout(3000)
                log("✅ Manus dashboard loaded.")
        except Exception as e:
            log(f"⚠️ manus login issue: {e}")
