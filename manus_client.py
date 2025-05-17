import asyncio
import os
import json
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError
from browserbase import Browserbase

load_dotenv()

# ---- Credentials & Config ----
MANUS_EMAIL = "pccagent18@gmail.com"
MANUS_PASSWORD = "thisisforpcc"
VERIFICATION_PHONE = "6263606593"

BB_API_KEY = os.getenv("BROWSERBASE_API_KEY")
BB_PROJECT_ID = os.getenv("BROWSERBASE_PROJECT_ID")

if not BB_API_KEY or not BB_PROJECT_ID:
    raise EnvironmentError("Missing BROWSERBASE_API_KEY or BROWSERBASE_PROJECT_ID in .env")

# Browserbase client (sync, but fine to create outside async context)
bb = Browserbase(api_key=BB_API_KEY)

class ManusClient:
    """Interact with Manus.AI via a cloud Playwright session running on Browserbase."""

    def ask_manus(self, prompt: str) -> str:
        """Public wrapper to make a blocking call from sync code."""
        return asyncio.run(self._interact_with_manus(prompt))

    async def _interact_with_manus(self, prompt: str) -> str:
        # Append END sentinel so we know when the model is finished
        prompt += " (say END when you're done writing your final answer)"
        print("🚀 Spinning up remote Chromium session on Browserbase…")

        # 1️⃣  Create a remote browser session
        session = bb.sessions.create(project_id=BB_PROJECT_ID)
        connect_url = session.connect_url
        print(f"🔗 Connected. Live view: https://browserbase.com/sessions/{session.id}")

        async with async_playwright() as p:
            # 2️⃣  Attach Playwright to the remote browser via CDP
            browser = await p.chromium.connect_over_cdp(connect_url)

            # 3️⃣  Grab or create the default context & page so recordings work
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()

            # ------- LOGIN FLOW (Google → Manus) -------
            if not os.path.exists("state.json"):
                await self._google_login(page, context)
            else:
                # Re‑use stored cookies to skip Google auth
                with open("state.json", "r", encoding="utf-8") as f:
                    state = json.load(f)
                    cookies = state.get("cookies", [])
                    if cookies:
                        await context.add_cookies(cookies)

            # Manus login via "Sign up with Google" button
            await self._manus_login(page)

            # Send the prompt and stream until we see "END"
            answer = await self._send_prompt(page, prompt)

            # Close out the remote browser (automatically ends the session)
            await browser.close()
            print("✅ Remote browser closed.")

            return answer

    # ---------------- Helper Methods ----------------

    async def _google_login(self, page, context):
        """Handle the Google SSO pop‑up the first time we run."""
        print("🔐 Performing one‑time Google login…")
        google = page  # Re‑use same page to minimise tab juggling
        await google.goto("https://accounts.google.com/signin/v2/identifier?service=mail")

        await google.fill('input[type="email"]', MANUS_EMAIL)
        await google.click('button:has-text("Next")')
        await google.wait_for_selector('input[type="password"]', timeout=10000)

        await google.fill('input[type="password"]', MANUS_PASSWORD)
        await google.click('button:has-text("Next")')
        await google.wait_for_timeout(5000)

        # Handle potential phone verification
        if await google.locator('input[type="tel"]').is_visible(timeout=5000):
            await google.fill('input[type="tel"]', VERIFICATION_PHONE)
            await google.keyboard.press("Enter")
            await google.wait_for_timeout(5000)

        # Save cookies for next run
        await context.storage_state(path="state.json")
        print("🔒 Google auth completed and cookies saved.")

    async def _manus_login(self, page):
        print("📄 Navigating to Manus login…")
        await page.goto("https://manus.im/login")
        try:
            google_btn = page.locator("text=Sign up with Google")
            if await google_btn.is_visible(timeout=5000):
                await google_btn.click()
                await page.wait_for_url("**/app", timeout=15000)
                await page.wait_for_timeout(3000)
                print("✅ Manus dashboard loaded.")
        except Exception as e:
            print(f"⚠️ Manus login issue: {e}")

    async def _send_prompt(self, page, prompt):
        print(f"🧠 Sending prompt → {prompt[:60]}…")
        await page.fill("textarea", prompt)
        await page.keyboard.press("Enter")

        print("📡 Waiting for END token…")
        seen_text = set()
        for _ in range(60):  # 60 × 2‑second polls = 2 minutes
            await page.wait_for_timeout(2000)
            blocks = await page.query_selector_all("div[data-message-id], div.prose")
            for block in blocks:
                try:
                    text = await block.inner_text()
                    if text not in seen_text:
                        print(f"💬 {text.strip()}")
                        seen_text.add(text)
                        if "END" in text:
                            return text.strip()
                except Exception:
                    continue
        return "[❌] Manus response did not include END in time."
