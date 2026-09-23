# Internal API used by other components (n8n, future agents) to query bot state.
# Runs on the internal Docker network (port 8001) but is protected regardless:
# every route requires header "X-API-Key" matching INTERNAL_API_KEY.

import os
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException

from claude_code_executor import get_recent_dev_log_entries

app = FastAPI()

_API_KEY = os.environ.get("INTERNAL_API_KEY")


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not _API_KEY:
        raise HTTPException(status_code=503, detail="INTERNAL_API_KEY no configurada en el servidor")
    if not x_api_key or not secrets.compare_digest(x_api_key, _API_KEY):
        raise HTTPException(status_code=401, detail="API key inválida o ausente")


@app.get("/dev-log", dependencies=[Depends(require_api_key)])
def dev_log(since_days: int = 7) -> list[dict]:
    return get_recent_dev_log_entries(since_days=since_days)
