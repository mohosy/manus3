
import asyncio, os, json, base64, hashlib
from typing import AsyncGenerator, Dict, Optional, List
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from browserbase import Browserbase

load_dotenv()

# ── credentials / config ───────────────────────────────────────
MANUS_EMAIL        = os.getenv("MANUS_EMAIL") or "pccagent18@gmail.com"
MANUS_PASSWORD     = os.getenv("MANUS_PASSWORD") or "thisisforpcc"
BB_API_KEY         = os.getenv("BROWSERBASE_API_KEY")
BB_PROJECT_ID      = os.getenv("BROWSERBASE_PROJECT_ID")

if not BB_API_KEY or not BB_PROJECT_ID:
    raise EnvironmentError("Missing BROWSERBASE_API_KEY or BROWSERBASE_PROJECT_ID")

# ── constants ──────────────────────────────────────────────────
INSTRUCTION = (
    "You are a Pasadena City College counselor. "
    "Answer the prompt below directly. Do NOT ask follow‑up questions. "
    "Here is the prompt:"
)

POLL_INTERVAL_MS = int(os.getenv("POLL_INTERVAL_MS", "5000"))   # 5‑second cadence
TIMEOUT_LOOPS    = int(os.getenv("TIMEOUT_LOOPS", "150"))       # ~12½ min max

bb = Browserbase(api_key=BB_API_KEY)

# ── main public class ──────────────────────────────────────────
class ManusClient:
    """Cloud‑browser wrapper around Manus.AI.
    Streams screenshots every POLL_INTERVAL_MS.
    If *use_vision=True* you rely on the caller (e.g. GPT‑4o Vision) to decide when
    the answer is ready. Otherwise we also sniff the DOM for an END token or
    any non-empty `div.prose` and return the text ourselves.
    """

    # --------- user-facing entry ---------
    async def stream_manus_frames(
        self,
        prompt: str,
        *,
        use_vision: bool = True
    ) -> AsyncGenerator[Dict[str, str], None]:
        """High-level wrapper that hides the auth flow."""
        async for chunk in self._stream_interact_with_manus(prompt, use_vision):
            yield chunk

    # -- legacy alias --------------------------------------------------
    async def stream_manus(self, prompt: str) -> AsyncGenerator[Dict[str, str], None]:
        async for c in self.stream_manus_frames(prompt):
            yield c

    # --------- internal helpers ---------
    async def _stream_interact_with_manus(
        self,
        prompt: str,
        use_vision: bool
    ) -> AsyncGenerator[Dict[str, str], None]:
        yield {"type": "log", "message": "🚀 spinning up remote chromium session…"}
        session = bb.start_session(project_id=BB_PROJECT_ID, headless=True)
        yield {"type": "log", "message": f"🔗 live view: https://browserbase.com/sessions/{session.id}"}

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(session.connect_url)
            context = browser.contexts[0]
            page    = context.pages[0]

            # 1) login
            await page.goto("https://manus.im/")
            await page.wait_for_selector('text=Sign in with Google')
            await page.click('text=Sign in with Google')
            await page.fill('input[type="email"]', MANUS_EMAIL)
            await page.click('button:has-text("Next")')
            await page.fill('input[type="password"]', MANUS_PASSWORD)
            await page.click('button:has-text("Next")')
            await page.wait_for_selector('textarea', timeout=60000)

            # 2) send prompt with instruction
            full_prompt = f"{INSTRUCTION}\n\n{prompt}"
            yield {"type": "log", "message": f"🧠 prompt → {prompt[:60]}…"}
            await page.fill("textarea", full_prompt)
            await page.keyboard.press("Enter")
            yield {"type": "log", "message": "📸 streaming screenshots …"}

            # 3) read loop
            seen_hash: Optional[str] = None
            for _ in range(TIMEOUT_LOOPS):
                await page.wait_for_timeout(POLL_INTERVAL_MS)

                # a) optional DOM sniff (if not using vision)
                if not use_vision:
                    try:
                        prose = await page.query_selector("div.prose")
                        if prose:
                            txt = (await prose.inner_text()).strip()
                            if txt:
                                yield {"type": "answer", "message": txt}
                                break
                    except Exception:
                        pass

                # b) screenshot stream
                png = await page.screenshot(full_page=True, type="png")
                b64 = base64.b64encode(png).decode()
                h   = hashlib.md5(b64.encode()).hexdigest()
                if h != seen_hash:
                    seen_hash = h
                    yield {"type": "frame", "b64": b64}

            else:
                yield {"type": "answer", "message": "[❌] Manus timed out."}

            await browser.close()
            session.close()
