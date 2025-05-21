

import asyncio
import os
import json
import re
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


# ── main public class ──────────────────────────────────────────
class ManusClient:
   """
   Cloud‑browser wrapper around Manus.AI.
   ask_manus(prompt)  -> {'logs': [...], 'answer': '...'}
   stream_manus(...)  -> async generator chunks (log / answer)
   """


   # -------- public (batch) --------
   def ask_manus(self, prompt: str) -> Dict[str, List[str]]:
       logs: List[str] = []


       def log(line: str):
           print(line)
           logs.append(line)


       answer = asyncio.run(self._interact_with_manus(prompt, log))
       return {"logs": logs, "answer": answer}


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
   async def _google_login(self, page, context, log):
       log("🔐 performing one‑time Google login…")
       await page.goto("https://accounts.google.com/signin/v2/identifier?service=mail")
       await page.fill('input[type="email"]', MANUS_EMAIL)
       await page.click('button:has-text("Next")')
       await page.wait_for_selector('input[type="password"]', state="attached", timeout=30000)
       await page.wait_for_selector('input[type="password"]', state="visible", timeout=30000)
       await page.fill('input[type="password"]', MANUS_PASSWORD)
       await page.click('button:has-text("Next")')
       await page.wait_for_timeout(5000)
       if await page.locator('input[type="tel"]').is_visible(timeout=5000):
           await page.fill('input[type="tel"]', VERIFICATION_PHONE)
           await page.keyboard.press("Enter")
           await page.wait_for_timeout(5000)
       await context.storage_state(path="state.json")
       log("🔒 google auth completed & cookies saved.")


   async def _google_login_stream(self, page, context):
       yield {"type": "log", "message": "🔐 performing one‑time Google login…"}
       await page.goto("https://accounts.google.com/signin/v2/identifier?service=mail")
       await page.fill('input[type="email"]', MANUS_EMAIL)
       await page.click('button:has-text("Next")')
       await page.wait_for_selector('input[type="password"]', state="attached", timeout=30000)
       await page.wait_for_selector('input[type="password"]', state="visible", timeout=30000)
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
       log("📄 navigating to Agent login…")
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


   async def _manus_login_stream(self, page):
       yield {"type": "log", "message": "📄 navigating to Agent login…"}
       await page.goto("https://manus.im/login")
       try:
           btn = page.locator("text=Sign up with Google")
           if await btn.is_visible(timeout=5000):
               await btn.click()
               await page.wait_for_url("**/app", timeout=15000)
               await page.wait_for_timeout(3000)
               yield {"type": "log", "message": "✅ Manus dashboard loaded."}
       except Exception as e:
           yield {"type": "log", "message": f"⚠️ agent login issue: {e}"}


   # -------- prompt helpers --------

   
async def _send_prompt(self, page, prompt, log) -> str:
   """Send *prompt* to Manus and return the final, END‑terminated answer."""
   log(f"🧠 sending prompt → {prompt[:60]}…")
   await page.fill("textarea", prompt)
   await page.keyboard.press("Enter")

   log("📡 Lance O' Lot is working… (waiting for END/ERROR token) 🐴")
   seen: Set[str] = set()
   parts: List[str] = []

   for _ in range(TIMEOUT_LOOPS):
      await page.wait_for_timeout(POLL_INTERVAL_MS)
      for block in await page.query_selector_all("div[data-message-id], div.prose"):
         try:
            raw = await self._wait_for_stable_text(block)
            txt = raw.strip()
            if not txt or txt in seen:
               continue
            seen.add(txt)
            parts.append(txt)
            log(f"💬 {txt[:80]}")
            if self._has_error(txt):
               return "[❌] Manus signaled ERROR."
            if self._has_end(txt):
               return self._strip_end_token("\n\n".join(parts))
         except Exception:
            continue
   return "[❌] Manus response timed out without END or ERROR."

async def _send_prompt_stream(self, page, prompt):
   """Stream Manus output for real‑time UI updates."""
   yield {"type": "log", "message": f"🧠 sending prompt → {prompt[:60]}…"}
   await page.fill("textarea", prompt)
   await page.keyboard.press("Enter")

   yield {"type": "log", "message": "📡 Lance O' Lot is working… (streaming) 🐴"}
   seen: Set[str] = set()
   parts: List[str] = []

   for _ in range(TIMEOUT_LOOPS):
      await page.wait_for_timeout(POLL_INTERVAL_MS)
      new_piece = False
      for block in await page.query_selector_all("div[data-message-id], div.prose"):
         try:
            raw = await self._wait_for_stable_text(block)
            txt = raw.strip()
            if not txt or txt in seen:
               continue
            seen.add(txt)
            parts.append(txt)
            new_piece = True
            yield {"type": "log", "message": f"💬 {txt[:80]}"}
         except Exception:
            continue

      if new_piece:
         yield {"type": "answer", "message": "\n\n".join(parts)}

      if parts and self._has_error(parts[-1]):
         yield {"type": "answer", "message": "[❌] Manus signaled ERROR."}
         return
      if parts and self._has_end(parts[-1]):
         yield {"type": "answer", "message": self._strip_end_token('\n\n'.join(parts))}
         return

   yield {"type": "answer", "message": "[❌] Manus response timed out without END or ERROR."}




   
   async def _wait_for_stable_text(self, block, timeout=3000, poll_interval=300):
       """Wait until the text inside *block* stops changing for two consecutive
       polls (default 300 ms) or until *timeout* ms is reached. Return the final
       stable text (may be empty)."""
       last_text = await block.inner_text()
       stable_count = 0
       elapsed = 0
       while elapsed < timeout:
           await asyncio.sleep(poll_interval / 1000)
           elapsed += poll_interval
           curr_text = await block.inner_text()
           if curr_text == last_text:
               stable_count += 1
               if stable_count >= 2:  # unchanged for two polls ⇒ stable
                   return curr_text
           else:
               stable_count = 0
               last_text = curr_text
       return last_text

   @staticmethod
   def _has_end(text: str) -> bool:
       return bool(re.search(r"(^|\s)END(\s|[.!?]|$)", text))



   @staticmethod
   def _has_error(text: str) -> bool:
       return bool(re.search(r"(^|\s)ERROR(\s|[.!?]|$)", text))


   @staticmethod
   def _strip_end_token(text: str) -> str:
       return re.sub(r"(^|\s)END(\s|[.!?]|$)", "", text).rstrip()