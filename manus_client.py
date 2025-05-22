
import asyncio, os, time, re
from typing import AsyncGenerator, Dict, Set
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from browserbase import Browserbase
import openai

load_dotenv()

# ── credentials ────────────────────────────────────────────────
MANUS_EMAIL        = "pccagent18@gmail.com"
MANUS_PASSWORD     = "thisisforpcc"
BB_API_KEY         = os.getenv("BROWSERBASE_API_KEY")
BB_PROJECT_ID      = os.getenv("BROWSERBASE_PROJECT_ID")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY")

if not all([BB_API_KEY, BB_PROJECT_ID, OPENAI_API_KEY]):
    raise EnvironmentError("Missing BB or OpenAI credentials")

bb = Browserbase(api_key=BB_API_KEY)
openai.api_key = OPENAI_API_KEY

# ── constants ──────────────────────────────────────────────────
POLL_MS                 = 2000      # 2‑second DOM poll
AI_CHECK_INTERVAL_SEC   = 15        # ask GPT‑4o every 15 s (max ~4x/min)
SPINNER_TEXT            = "Manus is working"
QUIET_POLLS_AFTER_SPIN  = 3
HARD_TIMEOUT_SEC        = 10 * 60

async def spinner_visible(page):
    try:
        return await page.locator(f"text={SPINNER_TEXT}").is_visible()
    except Exception:
        return False

async def collect_new(page, seen: Set[str]) -> list[str]:
    new = []
    for n in await page.query_selector_all("div[data-message-id], div.prose"):
        try:
            t = (await n.inner_text()).strip()
            if t and t not in seen:
                seen.add(t); new.append(t)
        except Exception:
            continue
    return new

async def ai_judge(user_prompt: str, answer_so_far: str) -> bool:
    msg = f"""
User prompt:
{user_prompt}

Draft answer so far:
{answer_so_far}

Is this draft COMPLETE, meaning no additional sentences are expected? Reply ONLY YES or NO."""
    res = await openai.ChatCompletion.acreate(
        model="gpt-4o-mini", messages=[{"role":"user","content":msg}], temperature=0
    )
    return res.choices[0].message.content.strip().upper().startswith("YES")

class ManusClient:
    async def stream_manus(self, prompt: str) -> AsyncGenerator[Dict[str,str], None]:
        async with async_playwright() as p:
            session = bb.sessions.create(project_id=BB_PROJECT_ID)
            browser = await p.chromium.connect_over_cdp(session.connect_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()
            yield {"type":"log","message":f"🔗 session: https://browserbase.com/sessions/{session.id}"}

            await page.goto("https://manus.im/app")
            await page.fill("textarea", prompt)
            await page.keyboard.press("Enter")

            seen, chunks = set(), []
            last_ai = time.monotonic()
            quiet_since_spinner, start = 0, time.monotonic()

            while True:
                await page.wait_for_timeout(POLL_MS)
                new_chunks = await collect_new(page, seen)
                for c in new_chunks:
                    chunks.append(c)
                    yield {"type":"log","message":c}
                    if "END" in c:
                        final = re.sub(r"\bEND\b","",c).strip()
                        yield {"type":"answer","message":final}
                        await browser.close(); return
                if not await spinner_visible(page):
                    quiet_since_spinner = quiet_since_spinner + 1 if not new_chunks else 0

                # AI check
                if (time.monotonic()-last_ai) >= AI_CHECK_INTERVAL_SEC and quiet_since_spinner >= QUIET_POLLS_AFTER_SPIN:
                    ans = "\n".join(chunks).strip()
                    try:
                        done = await ai_judge(prompt, ans)
                        yield {"type":"log","message":f"🤖 judge: {done}"}
                        if done:
                            yield {"type":"answer","message":ans}
                            await browser.close(); return
                    except Exception as e:
                        yield {"type":"log","message":f"judge error: {e}"}
                    last_ai = time.monotonic()

                if (time.monotonic()-start) > HARD_TIMEOUT_SEC:
                    yield {"type":"answer","message":"[❌] timeout"}
                    await browser.close(); return
