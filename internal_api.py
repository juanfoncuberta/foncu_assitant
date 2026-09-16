# SECURITY ASSUMPTION: this API is never exposed to the public internet.
# It listens only on the internal Docker network (port 8001) and has no
# authentication. Do not bind it to a public interface without adding auth.

from fastapi import FastAPI

from claude_code_executor import get_recent_dev_log_entries

app = FastAPI()


@app.get("/dev-log")
def dev_log(since_days: int = 7) -> list[dict]:
    return get_recent_dev_log_entries(since_days=since_days)
