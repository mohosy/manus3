from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import asyncio
import os

from manus_client import ManusClient

# ── FastAPI setup ──────────────────────────────────────────────
app = FastAPI(
    title="PCC Agent Backend",
    version="1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# 🔐  CORS – domains allowed to embed the chat widget
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://lansoai.com",       # standalone bot site
        "https://www.pasadena.edu",  # PCC main site
        "https://pasadena.edu",      # root domain (no www)
    ],
    allow_methods=["POST", "OPTIONS"],   # ← include OPTIONS for pre-flight
    allow_headers=["*"],
)

# single Manus agent instance per process
agent = ManusClient()

# ── request / response schemas ─────────────────────────────────
class AskRequest(BaseModel):
    prompt: str

class AskResponse(BaseModel):
    logs:   List[str]
    answer: str

# ── route ─────────────────────────────────────────────────────
@app.post("/ask", response_model=AskResponse)
async def ask_endpoint(req: AskRequest):
    """
    Forward the student's prompt to Manus and return:
      { logs: [...status lines...], answer: "...final text..." }
    """
    loop = asyncio.get_event_loop()
    try:
        payload = await loop.run_in_executor(None, agent.ask_manus, req.prompt)
        return payload
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── local dev entrypoint ──────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=True,
    )
