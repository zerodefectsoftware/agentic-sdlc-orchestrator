"""FastAPI application entrypoint for the URL shortener.

Currently a toolchain skeleton: only the health endpoint exists. Shorten/resolve/analytics
routes are added by the orchestrator runs, not hand-written here.
"""

from fastapi import FastAPI

app = FastAPI(title="URL Shortener", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
