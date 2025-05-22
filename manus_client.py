
import asyncio
import os
import json
import base64
import hashlib
from typing import AsyncGenerator, Dict, List
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

# ── timing ─────────────────────────────────────────────────────
POLL_INTERVAL_MS = 2000          # 2‑second initial poll
TIMEOUT_LOOPS    = 150           # ~5 minutes max

# ── main public class ──────────────────────────────────────────
class ManusClient:
    """
    Cloud‑browser wrapper around Manus.AI **v2**
    Streams full‑page screenshots so a vision model (GPT‑4o‑Vision)
    can decide when the answer is complete.

    • stream_manus_frames(prompt)  -> async generator yielding:
        {"type":"log",   "message":"..."}
        {"type":"frame", "b64":"<base64‑png>"}

    NOTE: the old END‑token logic is removed.
    """
    # --- backward‑compat alias (old code expects stream_manus) ---
    async def stream_manus(self, prompt: str):
        """Alias for stream_manus_frames for legacy callers."""
        async for chunk in self.stream_manus_frames(prompt):
            yield chunk


# --- backward‑compat alias (old code expects stream_manus) ---
async def stream_manus(self, prompt: str):
    """Alias for stream_manus_frames for legacy callers."""
    async for chunk in self.stream_manus_frames(prompt):
        yield chunk


    # ------------- public (stream) -------------
    async def stream_manus_frames(self, prompt: str) -> AsyncGenerator[Dict[str, str], None]:
        async for chunk in self._stream_interact_with_manus(prompt):
            yield chunk

    # ------------- internal (stream path) ------------
    async def _stream_interact_with_manus(self, prompt: str) -> AsyncGenerator[Dict[str, str], None]:
        yield {"type": "log", "message": "🚀 spinning up remote chromium session on Browserbase…"}

        session = bb.sessions.create(project_id=BB_PROJECT_ID)
        yield {"type": "log", "message": f"🔗 connected. live view: https://browserbase.com/sessions/{session.id}"}

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(session.connect_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page    = context.pages[0]   if context.pages   else await context.new_page()

            # ── login flow ──
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

            # ── prompt / answer (stream screenshots) ──
            async for chunk in self._send_prompt_stream(page, prompt):
                yield chunk

            await browser.close()
            yield {"type": "log", "message": "✅ remote browser closed."}

    # ------------- helpers – auth -------------
    async def _google_login_stream(self, page, context) -> AsyncGenerator[Dict[str, str], None]:
        yield {"type": "log", "message": "🔐 performing one-time Google login…"}
        await page.goto("https://accounts.google.com/signin/v2/identifier?service=mail")
        await page.fill('input[type="email"]', MANUS_EMAIL)
        await page.click('button:has-text("Next")')
        await page.wait_for_selector('input[type="password"]', timeout=10000)
        await page.fill('input[type="password"]', MANUS_PASSWORD)
        await page.click('button:has-text("Next")')
        await page.wait_for_timeout(5000)

        # phone 2FA
        if await page.locator('input[type="tel"]').is_visible(timeout=5000):
            await page.fill('input[type="tel"]', VERIFICATION_PHONE)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(5000)

        await context.storage_state(path="state.json")
        yield {"type": "log", "message": "🔒 google auth completed & cookies saved."}

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

    # ------------- helpers – prompt & screenshots -------------
    async def _send_prompt_stream(self, page, prompt: str) -> AsyncGenerator[Dict[str, str], None]:
        yield {"type": "log", "message": f"🧠 sending prompt → {prompt[:60]}…"}
        await page.fill("textarea", prompt)
        await page.keyboard.press("Enter")

        yield {"type": "log", "message": "📡 streaming full‑page screenshots …"}

        seen_hash = None
        poll_ms   = POLL_INTERVAL_MS

        for _ in range(TIMEOUT_LOOPS):
            await page.wait_for_timeout(poll_ms)

            png_bytes = await page.screenshot(full_page=True, type="png")
            b64 = base64.b64encode(png_bytes).decode()
            h   = hashlib.md5(b64.encode()).hexdigest()

            if h != seen_hash:
                seen_hash = h
                yield {"type": "frame", "b64": b64}

            # exponential back‑off every 60 secs (30 loops)
            if (_ + 1) % 30 == 0 and poll_ms < 8000:
                poll_ms += 2000

        yield {"type": "log", "message": "⌛ timeout reached without external FINAL verdict."}
