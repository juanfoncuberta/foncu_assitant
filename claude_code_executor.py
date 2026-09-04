"""
Executes development tasks via Claude Code CLI in headless (non-interactive) mode.
"""

import json
import logging
import os
import subprocess

TIMEOUT_SECONDS = 600  # 10 minutes

logger = logging.getLogger(__name__)


def check_git_status(directory_path: str) -> dict:
    """
    Returns {'is_git': bool, 'is_clean': bool}.
    is_clean is True only when the working tree has no uncommitted changes.
    """
    if not os.path.exists(os.path.join(directory_path, ".git")):
        return {"is_git": False, "is_clean": False}
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=directory_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        is_clean = result.returncode == 0 and result.stdout.strip() == ""
        return {"is_git": True, "is_clean": is_clean}
    except Exception:
        return {"is_git": True, "is_clean": False}


def get_git_diff(directory_path: str) -> str:
    """Returns the combined staged+unstaged diff after task execution, truncated to 5000 chars."""
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=directory_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        diff = result.stdout.strip()
        if not diff:
            return "(sin cambios en el árbol de trabajo)"
        return diff[:5000] + ("…(truncado)" if len(diff) > 5000 else "")
    except Exception:
        return "(no se pudo obtener el diff)"


def execute_task(task_content: str, directory_path: str) -> dict:
    """
    Invokes Claude Code headless with task_content as the prompt, working inside directory_path.
    Returns a dict with: result, cost_usd, session_id, error.
    Never raises — all failures are captured in the 'error' key.
    """
    try:
        proc = subprocess.run(
            [
                "claude",
                "-p", task_content,
                "--output-format", "json",
                "--permission-mode", "acceptEdits",
                "--add-dir", directory_path,
            ],
            cwd=directory_path,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.error("Claude Code timed out after %ds", TIMEOUT_SECONDS)
        return {
            "error": f"Timeout: Claude Code exceeded {TIMEOUT_SECONDS}s without finishing.",
            "result": None,
            "cost_usd": None,
            "session_id": None,
        }
    except FileNotFoundError:
        logger.error("'claude' binary not found in PATH")
        return {
            "error": "'claude' command not found. Is Claude Code installed and in PATH?",
            "result": None,
            "cost_usd": None,
            "session_id": None,
        }
    except Exception as exc:
        logger.exception("Unexpected error launching Claude Code")
        return {
            "error": str(exc),
            "result": None,
            "cost_usd": None,
            "session_id": None,
        }

    if proc.returncode != 0:
        logger.error("Claude Code exited %d. stderr: %s", proc.returncode, proc.stderr[:500])
        return {
            "error": f"Claude Code exited with code {proc.returncode}.",
            "stderr": proc.stderr[:2000],
            "result": None,
            "cost_usd": None,
            "session_id": None,
        }

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.error("Non-JSON output from Claude Code: %s", proc.stdout[:300])
        return {
            "error": "Claude Code output was not valid JSON.",
            "raw_output": proc.stdout[:2000],
            "result": None,
            "cost_usd": None,
            "session_id": None,
        }

    return {
        "result": data.get("result"),
        "cost_usd": data.get("cost_usd"),
        "session_id": data.get("session_id"),
        "git_diff": get_git_diff(directory_path),
        "error": None,
    }
