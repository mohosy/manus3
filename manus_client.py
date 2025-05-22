
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

# ── instructions sent before every prompt ─────────────────────
INSTRUCTION = (
    """You are a Pasadena City College counselor. Answer this prompt, but do not ask
follow‑up questions back—just work and answer it.

Here is the prompt:
""")

# ── timing ─────────────────────────────────────────────────────
POLL_INTERVAL_MS = 10_000        # 10‑second polling between screenshots
TIMEOUT_LOOPS    = 150           # 150 × 10 s  ≈ 25 minutes max per prompt

# ── main public class ──────────────────────────────────────────
class ManusClient:
    """Cloud‑browser wrapper around Manus.AI (v2).

    The generator streams full‑page screenshots so a vision model
    (e.g. GPT‑4o‑Vision) can decide when the answer is done.

    Yields dict chunks of two kinds:

        {"type": "log",   "message": "…"}
        {"type": "frame", "b64": "<base64‑png>"}
    """

    # ---------- backward‑compat alias (old code expects stream_manus) ----------
    async def stream_manus(self, prompt: str):
        """Alias kept for legacy callers."""
        async for chunk in self.stream_manus_frames(prompt):
            yield chunk

    # ------------------------ public API ------------------------
    async def stream_manus_frames(self, prompt: str) -> AsyncGenerator[Dict[str, str], None]:
        """Entry point: returns an async generator of log/frame chunks."""
        async for chunk in self._stream_interact_with_manus(prompt):
            yield chunk

    # -------------------- internal workflow --------------------
    async def _stream_interact_with_manus(self, prompt: str) -> AsyncGenerator[Dict[str, str], None]:
        yield {"type": "log", "message": "🚀 spinning up remote Chromium session on Browserbase…"}

        session = bb.sessions.create(project_id=BB_PROJECT_ID)
        yield {"type": "log", "message": f"🔗 connected. live view: https://browserbase.com/sessions/{session.id}"}

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(session.connect_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page    = context.pages[0]   if context.pages   else await context.new_page()

            # ── login flow (Google, then Manus) ──
            if not os.path.exists("state.json"):
                async for log in self._google_login_stream(page, context):
                    yield log
            else:
                # reuse cookies for faster login
                with open("state.json", "r", encoding="utf-8") as f:
                    cookies = json.load(f).get("cookies", [])
                if cookies:
                    await context.add_cookies(cookies)
                    yield {"type": "log", "message": "🔓 cookies loaded from state.json."}

            async for log in self._manus_login_stream(page):
                yield log

            # ── send prompt & stream screenshots ──
            async for chunk in self._send_prompt_stream(page, prompt):
                yield chunk

            await browser.close()
            yield {"type": "log", "message": "✅ remote browser closed."}

    # ------------------ helpers – authentication ------------------
    async def _google_login_stream(self, page, context) -> AsyncGenerator[Dict[str, str], None]:
        yield {"type": "log", "message": "🔐 performing one‑time Google login…"}

        await page.goto("https://accounts.google.com/signin/v2/identifier?service=mail", timeout=60_000)
        await page.fill('input[type="email"]', MANUS_EMAIL)
        await page.click('button:has-text("Next")')

        await page.wait_for_selector('input[type="password"]', timeout=30_000)
        await page.fill('input[type="password"]', MANUS_PASSWORD)
        await page.click('button:has-text("Next")')

        # Handle optional phone 2FA
        try:
            if await page.locator('input[type="tel"]').is_visible(timeout=5_000):
                await page.fill('input[type="tel"]', VERIFICATION_PHONE)
                await page.keyboard.press("Enter")
        except Exception:
            pass  # no phone challenge

        # buffer for redirects / inbox load
        await page.wait_for_timeout(5_000)

        # persist cookies for next runs
        await context.storage_state(path="state.json")
        yield {"type": "log", "message": "🔒 Google auth completed & cookies saved."}

    async def _manus_login_stream(self, page) -> AsyncGenerator[Dict[str, str], None]:
        yield {"type": "log", "message": "📄 navigating to Manus login…"}

        await page.goto("https://manus.im/login", timeout=30_000)

        # perform "Sign in with Google"
        try:
            btn = page.locator("text=Sign up with Google")
            await btn.wait_for(state="visible", timeout=10_000)
            await btn.click()
            await page.wait_for_url("**/app", timeout=30_000)
            await page.wait_for_timeout(3_000)
            yield {"type": "log", "message": "✅ Manus dashboard loaded."}
        except Exception as e:
            yield {"type": "log", "message": f"⚠️ Manus login issue: {e}"}

    # ---------------- helper – prompt & screenshots ----------------
    async def _send_prompt_stream(self, page, prompt: str) -> AsyncGenerator[Dict[str, str], None]:
        message = f"{INSTRUCTION}\n{prompt}"
        yield {"type": "log", "message": f"🧠 sending prompt → {prompt[:60]}…"}

        await page.fill("textarea", message)
        await page.keyboard.press("Enter")

        yield {"type": "log", "message": "📡 streaming full‑page screenshots every 10 s…"}

        seen_hash: str | None = None

        for _ in range(TIMEOUT_LOOPS):
            await page.wait_for_timeout(POLL_INTERVAL_MS)

            png_bytes = await page.screenshot(full_page=True, type="png")
            b64 = base64.b64encode(png_bytes).decode("ascii")
            h   = hashlib.md5(b64.encode()).hexdigest()

            if h != seen_hash:
                seen_hash = h
                yield {"type": "frame", "b64": b64}

        yield {"type": "log", "message": "⌛ timeout reached without external FINAL verdict."}
