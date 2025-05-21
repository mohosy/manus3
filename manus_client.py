

import asyncio
import os
import json
from typing import AsyncGenerator, Dict
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
   Cloud‑browser wrapper around Manus.AI.


   • ask_manus(prompt)        -> {"logs": [...], "answer": "..."}
     (returns everything at once, back‑compat.)


   • stream_manus(prompt) -> async generator yielding:
       {"type": "log",    "message": "..."}      # for each thinking blurb
       {"type": "answer", "message": "final"}    # final answer, END removed
   """


   # ------------- public (batch) -------------
   # ── helper: robust element utils ────────────────────────────
   async def _fill_first_visible(self, page, selectors, text) -> bool:
       """Try filling each selector until one is visible. Returns True if filled."""
       for sel in selectors:
           try:
               loc = page.locator(sel)
               await loc.wait_for(state="visible", timeout=2500)
               await loc.fill(text)
               return True
           except Exception:
               continue
       return False

   async def _click_first(self, page, selectors) -> None:
       """Click the first visible matching selector."""
       for sel in selectors:
           try:
               loc = page.locator(sel)
               await loc.wait_for(state="visible", timeout=2500)
               await loc.click()
               return
           except Exception:
               continue
       raise RuntimeError(f"Could not click any of: {selectors}")


   def ask_manus(self, prompt: str) -> Dict[str, list]:
       logs: list[str] = []


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


   # ------------- internal (stream path) ------------
   async def _stream_interact_with_manus(self, prompt: str) -> AsyncGenerator[Dict[str, str], None]:
       prompt += " (say END when you're done writing your final answer)"
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


           # ── prompt / answer ──
           async for chunk in self._send_prompt_stream(page, prompt):
               yield chunk


           await browser.close()
           yield {"type": "log", "message": "✅ remote browser closed."}


   # ------------- helpers -------------

   async def _google_login(self, page, context, log):
       log("🔐 performing one‑time Google login…")
       await page.goto("https://accounts.google.com/signin/v2/identifier?service=mail")

       email_selectors = [
           'input[type="email"]',
           'input[name="identifier"]',
           'input[autocomplete="username"]',
           'input[type="text"][name="identifier"]'
       ]
       if not await self._fill_first_visible(page, email_selectors, MANUS_EMAIL):
           raise RuntimeError("Email input not found 🌩️")

       await self._click_first(page, ['button:has-text("Next")', 'button:has-text("next")'])

       password_selectors = [
           'input[type="password"]',
           'input[name="Passwd"]',
           'input[autocomplete="current-password"]'
       ]
       await page.wait_for_timeout(1000)
       if not await self._fill_first_visible(page, password_selectors, MANUS_PASSWORD):
           raise RuntimeError("Password input not found 🌩️")

       await self._click_first(page, ['button:has-text("Next")', 'button:has-text("Sign in")'])

       # optional phone verification
       try:
           await page.locator('input[type="tel"]').wait_for(state="visible", timeout=5000)
           await page.fill('input[type="tel"]', VERIFICATION_PHONE)
           await page.keyboard.press("Enter")
       except Exception:
           pass

       await page.wait_for_timeout(3000)

       # save cookies/state for future sessions
       state = await context.storage_state()
       with open("state.json", "w", encoding="utf-8") as f:
           json.dump(state, f)
           log("💾 cookies saved to state.json (good for 3‑6 months).")

   async def _google_login_stream(self, page, context) -> AsyncGenerator[Dict[str, str], None]:
       yield {"type": "log", "message": "🔐 performing one‑time Google login…"}
       await page.goto("https://accounts.google.com/signin/v2/identifier?service=mail")

       email_selectors = [
           'input[type="email"]',
           'input[name="identifier"]',
           'input[autocomplete="username"]',
           'input[type="text"][name="identifier"]'
       ]
       if not await self._fill_first_visible(page, email_selectors, MANUS_EMAIL):
           yield {"type": "error", "message": "[❌] Email input not found"}
           return
       yield {"type": "log", "message": "📧 entered email"}

       await self._click_first(page, ['button:has-text("Next")', 'button:has-text("next")'])

       password_selectors = [
           'input[type="password"]',
           'input[name="Passwd"]',
           'input[autocomplete="current-password"]'
       ]
       await page.wait_for_timeout(1000)
       if not await self._fill_first_visible(page, password_selectors, MANUS_PASSWORD):
           yield {"type": "error", "message": "[❌] Password input not found"}
           return
       yield {"type": "log", "message": "🔒 entered password"}

       await self._click_first(page, ['button:has-text("Next")', 'button:has-text("Sign in")'])

       # phone verification
       try:
           await page.locator('input[type="tel"]').wait_for(state="visible", timeout=5000)
           await page.fill('input[type="tel"]', VERIFICATION_PHONE)
           await page.keyboard.press("Enter")
           yield {"type": "log", "message": "📱 phone verification entered"}
       except Exception:
           pass

       await page.wait_for_timeout(3000)

       state = await context.storage_state()
       with open("state.json", "w", encoding="utf-8") as f:
           json.dump(state, f)
           yield {"type": "log", "message": "💾 cookies saved to state.json"}

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




   async def _send_prompt(self, page, prompt, log) -> str:
       log(f"🧠 sending prompt → {prompt[:60]}…")
       await page.fill("textarea", prompt)
       await page.keyboard.press("Enter")


       log("📡 waiting for END token…")
       seen = set()
       for _ in range(60):                # 2‑minute timeout
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


   async def _send_prompt_stream(self, page, prompt) -> AsyncGenerator[Dict[str, str], None]:
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


       for _ in range(60):                # 2‑minute timeout
           await page.wait_for_timeout(2000)
           async for txt in new_texts():
               yield {"type": "log", "message": f"💬 {txt}"}
               if "END" in txt:
                   clean = txt.replace("END", "").strip()
                   yield {"type": "answer", "message": clean}
                   return
       yield {"type": "answer", "message": "[❌] Manus response did not include END in time."}
