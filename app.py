from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import os

from manus_client import ManusClient

# ── FastAPI setup ──────────────────────────────────────────────
app = FastAPI(title="PCC Agent Backend", version="1.0", docs_url="/docs", redoc_url="/redoc")

# 🔐  CORS – domains allowed to embed the chat widget
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://lansoai.com",          # standalone bot site
        "https://www.pasadena.edu",     # PCC main site
        "https://pasadena.edu"           # root domain (no www)
    ],
    allow_methods=["POST"],
    allow_headers=["*"]
)

# instantiate Manus agent once per process
agent = ManusClient()

# ── request / response schemas ─────────────────────────────────
class AskRequest(BaseModel):
    prompt: str

class AskResponse(BaseModel):
    answer: str

# ── route ─────────────────────────────────────────────────────
@app.post("/ask", response_model=AskResponse)
async def ask_endpoint(req: AskRequest):
    """Forward the student's prompt to Manus and return the answer."""
    loop = asyncio.get_event_loop()
    try:
        # run blocking call in a separate thread
        answer = await loop.run_in_executor(None, agent.ask_manus, req.prompt)
        return {"answer": answer}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── local dev entrypoint ──────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
