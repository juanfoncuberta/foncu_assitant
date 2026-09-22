"""
Executes development tasks via Claude Code CLI in headless (non-interactive) mode.
"""

import json
import logging
import os
import re
import sqlite3
import subprocess
import unicodedata
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("DB_PATH", "assistant.db")

TIMEOUT_SECONDS = 600  # 10 minutes

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# dev_log — persists Level-2 execution summaries (self-contained, no foreign deps)
# ---------------------------------------------------------------------------


def _dev_log_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dev_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            task_content TEXT    NOT NULL,
            branch       TEXT    NOT NULL,
            summary      TEXT    NOT NULL,
            created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(dev_log)")}
    if "plan" not in existing_cols:
        conn.execute("ALTER TABLE dev_log ADD COLUMN plan TEXT NOT NULL DEFAULT ''")
    conn.commit()
    return conn


def _save_dev_log(task_content: str, branch: str, summary: str, plan: str = "") -> None:
    with _dev_log_conn() as conn:
        conn.execute(
            "INSERT INTO dev_log (task_content, branch, summary, plan) VALUES (?, ?, ?, ?)",
            (task_content, branch, summary, plan),
        )


def get_recent_dev_log_entries(since_days: int = 7) -> list[dict]:
    with _dev_log_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, task_content, branch, summary, plan, created_at
            FROM dev_log
            WHERE created_at >= datetime('now', ?)
            ORDER BY id DESC
            """,
            (f"-{since_days} days",),
        ).fetchall()
    return [
        {
            "id": r[0],
            "task_content": r[1],
            "branch": r[2],
            "summary": r[3],
            "plan": r[4],
            "created_at": r[5],
        }
        for r in rows
    ]


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


# ---------------------------------------------------------------------------
# Branch-based execution helpers (Level-2 flow)
# ---------------------------------------------------------------------------

PLAN_TIMEOUT_SECONDS = 120

_PLAN_PROMPT_TEMPLATE = """You are about to perform the following development task, but you must NOT \
make any changes yet — this is a planning-only step.

Describe, in 2-4 concise sentences, the plan you would follow: which files you would touch \
and what you would change in each.

Task:
{task_content}"""


def _get_plan(task_content: str, directory_path: str) -> str:
    """
    Read-only planning call (no Edit/Write/Bash tools): asks Claude Code to describe the
    approach it would take *before* any code is touched, so the plan can later be compared
    against the real diff. Never raises — returns a fallback string on any failure.
    """
    try:
        proc = subprocess.run(
            [
                "claude",
                "-p", _PLAN_PROMPT_TEMPLATE.format(task_content=task_content),
                "--output-format", "json",
                "--disallowedTools", "Edit,Write,Bash",
                "--add-dir", directory_path,
            ],
            cwd=directory_path,
            capture_output=True,
            text=True,
            timeout=PLAN_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception("Fallo al obtener el plan previo a la ejecución")
        return "(no se pudo obtener el plan previo)"

    if proc.returncode != 0:
        logger.warning("Plan previo: claude -p exited %d", proc.returncode)
        return "(no se pudo obtener el plan previo)"

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return "(no se pudo obtener el plan previo)"

    return (data.get("result") or "").strip() or "(plan vacío)"


_FIX_WORDS = {
    "fix", "bug", "error", "broken", "crash", "patch",
    "arregla", "arreglar", "corrige", "corregir", "falla", "repara", "reparar",
}
_FEAT_WORDS = {
    "add", "new", "create", "implement", "introduce",
    "añade", "añadir", "crea", "crear", "agrega", "agregar",
    "implementa", "implementar", "nueva", "nuevo",
}
_STOP_WORDS = {
    "a", "an", "the", "in", "on", "at", "to", "for", "of", "and", "or", "with",
    "el", "la", "los", "las", "un", "una", "de", "del", "en", "con",
    "por", "para", "que", "se", "es", "al", "y", "o", "su",
}


def _git(args: list, cwd: str, timeout: int = 30) -> tuple:
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _classify_task(task_content: str) -> str:
    words = set(re.sub(r"[^\w\s]", " ", task_content.lower()).split())
    if words & _FIX_WORDS:
        return "fix"
    if words & _FEAT_WORDS:
        return "feat"
    return "chore"


def _make_branch_name(task_content: str) -> str:
    prefix = _classify_task(task_content)
    normalized = unicodedata.normalize("NFD", task_content.lower())
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    all_words = re.sub(r"[^\w\s]", " ", ascii_text).split()
    meaningful = [w for w in all_words if w not in _STOP_WORDS and len(w) > 2]
    slug = re.sub(r"[^a-z0-9]+", "-", " ".join(meaningful[:4])).strip("-") or "task"
    suffix = datetime.now(timezone.utc).strftime("%H%M%S")
    return f"{prefix}/{slug}-{suffix}"


_COMMIT_INSTRUCTIONS = """
After completing the task, if you made any changes, commit them:
  git add -A
  git commit -m "<message>"

Commit message rules (follow strictly):
- language: English
- style: imperative mood, all lowercase (e.g. "add login handler", "fix null check in parser")
- max 72 characters
- describe what changed in the code, not the task description
- no "Co-authored-by", no "Generated with", no signatures of any kind

In your final response, include 2-4 sentences describing: what the problem or goal was,
what you changed, and why that approach solves it."""


def _fallback_commit_message(branch_name: str) -> str:
    # Derive a plain English message from the branch slug, avoiding Spanish task text.
    # "feat/add-docstring-main-143215" -> "add docstring main"
    slug = branch_name.split("/", 1)[-1]
    slug = re.sub(r"-\d{6}$", "", slug)  # strip HHMMSS suffix
    return slug.replace("-", " ")


def _fallback_commit(branch_name: str, directory_path: str) -> str | None:
    """
    Fallback used only when claude -p did not commit despite leaving changes.
    Stages everything and commits with a generic English message derived from the branch name.
    Returns None on success, error string on failure.
    """
    rc, porcelain, _ = _git(["status", "--porcelain"], directory_path)
    if rc != 0 or not porcelain:
        return None  # nothing to commit

    rc, _, err = _git(["add", "-A"], directory_path)
    if rc != 0:
        return f"git add falló: {err}"

    rc, _, err = _git(["commit", "-m", _fallback_commit_message(branch_name)], directory_path)
    if rc != 0:
        return f"git commit falló: {err}"

    return None


def _checkout_safe(branch: str, directory_path: str) -> None:
    """
    Returns to branch. If the repo is dirty (uncommitted changes remain after a failed
    auto-commit), force-checkout to avoid carrying those changes into the original branch.
    """
    rc, porcelain, _ = _git(["status", "--porcelain"], directory_path)
    is_dirty = rc == 0 and bool(porcelain)

    if is_dirty:
        # Discard residual changes — they're already captured in the result's git_diff.
        logger.warning(
            "Repo sucio al volver a '%s'; descartando cambios sin commit antes del checkout",
            branch,
        )
        rc, _, err = _git(["checkout", "-f", branch], directory_path)
    else:
        rc, _, err = _git(["checkout", branch], directory_path)

    if rc != 0:
        logger.critical("No se pudo volver a la rama '%s': %s", branch, err)


def execute_task_on_branch(task_content: str, directory_path: str) -> dict:
    """
    Level-2 flow: creates a typed branch (feat/fix/chore), runs the task there asking
    claude -p to commit its own changes with a proper English message, then always
    returns to the original branch. Never merges to main.

    Extra keys in the returned dict:
      branch            — name of the created branch
      plan              — plan described by a read-only claude -p call before any change,
                           made for the reviewer subagent to compare against the real diff
      auto_commit_error — present only if the fallback commit step was needed and failed
    """
    rc, original_branch, err = _git(["rev-parse", "--abbrev-ref", "HEAD"], directory_path)
    if rc != 0:
        return {
            "error": f"No se pudo obtener la rama actual: {err}",
            "result": None,
            "cost_usd": None,
            "session_id": None,
            "branch": None,
            "git_diff": None,
        }

    branch_name = _make_branch_name(task_content)
    rc, _, err = _git(["checkout", "-b", branch_name], directory_path)
    if rc != 0:
        return {
            "error": f"No se pudo crear la rama '{branch_name}': {err}",
            "result": None,
            "cost_usd": None,
            "session_id": None,
            "branch": None,
            "git_diff": None,
        }

    plan = _get_plan(task_content, directory_path)

    enriched_prompt = task_content + _COMMIT_INSTRUCTIONS

    try:
        result = execute_task(enriched_prompt, directory_path)
        result["branch"] = branch_name
        result["plan"] = plan

        # Replace git_diff with a diff against the original branch so it captures
        # committed changes too (git diff HEAD only shows uncommitted changes).
        rc, branch_diff, _ = _git(["diff", original_branch], directory_path, timeout=15)
        if rc == 0:
            diff = branch_diff[:5000] + ("…(truncado)" if len(branch_diff) > 5000 else "")
            result["git_diff"] = diff or "(sin cambios respecto a la rama original)"

        # Fallback: if claude -p left changes uncommitted, commit them with a generic message.
        if result.get("error") is None:
            rc, porcelain, _ = _git(["status", "--porcelain"], directory_path)
            if rc == 0 and porcelain:
                logger.warning(
                    "claude -p no comiteó en '%s'; aplicando commit de fallback", branch_name
                )
                commit_err = _fallback_commit(branch_name, directory_path)
                if commit_err:
                    logger.error("Fallback commit fallido en '%s': %s", branch_name, commit_err)
                    result["auto_commit_error"] = commit_err

            _save_dev_log(task_content, branch_name, result.get("result") or "", plan=plan)
    finally:
        _checkout_safe(original_branch, directory_path)

    return result
