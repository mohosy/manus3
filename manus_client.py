import asyncio
import os
import json
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

# ── main public class ──────────────────────────────────────────
class ManusClient:
    """
    cloud-browser wrapper around Manus.AI.
    ask_manus(prompt)  →  {"logs": [...], "answer": "..."}
    """

    # ------------- public -------------
    def ask_manus(self, prompt: str) -> dict:
        logs: list[str] = []

        # helper that prints AND stores
        def log(line: str):
            print(line)
            logs.append(line)

        answer = asyncio.run(self._interact_with_manus(prompt, log))
        return {"logs": logs, "answer": answer}

    # ------------- internal ------------
    async def _interact_with_manus(self, prompt: str, log) -> str:
        prompt += " (say END when you're done writing your final answer)"
        log("🚀 spinning up remote chromium session on Browserbase…")

        session = bb.sessions.create(project_id=BB_PROJECT_ID)
        log(f"🔗 connected. live view: https://browserbase.com/sessions/{session.id}")

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(session.connect_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page    = context.pages[0]   if context.pages   else await context.new_page()

            # ── login flow ──
            if not os.path.exists("state.json"):
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

    # ------------- helpers -------------
    async def _google_login(self, page, context, log):
        log("🔐 performing one-time Google login…")
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

    async def _send_prompt(self, page, prompt, log) -> str:
        log(f"🧠 sending prompt → {prompt[:60]}…")
        await page.fill("textarea", prompt)
        await page.keyboard.press("Enter")

        log("📡 waiting for END token…")
        seen = set()
        for _ in range(60):                # 2-minute timeout
            await page.wait_for_timeout(2000)
            for block in await page.query_selector_all("div[data-message-id], div.prose"):
                try:
                    text = await block.inner_text()
                    if text not in seen:
                        seen.add(text)
                        log(f"💬 {text.strip()}")
                        if "END" in text:
                            return text.strip()
                except Exception:
                    continue
        return "[❌] Manus response did not include END in time."
