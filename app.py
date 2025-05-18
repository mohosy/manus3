
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, AsyncGenerator
import asyncio
import json
import os

from manus_client import ManusClient

# ── FastAPI setup ──────────────────────────────────────────────
app = FastAPI(
    title="PCC Agent Backend",
    version="1.1",
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
    allow_methods=["GET", "POST", "OPTIONS"],   # include GET for stream
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

# ── /ask – batch response (legacy) ────────────────────────────
@app.post("/ask", response_model=AskResponse)
async def ask_endpoint(req: AskRequest):
    """Forward the student's prompt to Manus (batch) and return logs + answer."""
    loop = asyncio.get_event_loop()
    try:
        payload = await loop.run_in_executor(None, agent.ask_manus, req.prompt)
        return payload
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── /ask_stream – live streaming response ─────────────────────
@app.post("/ask_stream")
async def ask_stream_endpoint(req: AskRequest):
    """Stream thinking logs + final answer in real‑time (NDJSON)."""

    async def stream() -> AsyncGenerator[str, None]:
        try:
            async for chunk in agent.stream_manus(req.prompt):
                yield json.dumps(chunk, ensure_ascii=False) + "\n"
        except Exception as ex:
            err = {"type": "error", "message": str(ex)}
            yield json.dumps(err, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="text/plain; charset=utf-8")

# ── local dev entrypoint ──────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=True,
    )
