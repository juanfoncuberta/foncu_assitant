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

import project_directory_map
import usage_log

DB_PATH = os.environ.get("DB_PATH", "assistant.db")

TIMEOUT_SECONDS = 600  # 10 minutes

# Modelo por paso. Los pasos de planificacion y revision son de solo lectura y mucho
# mas simples que la ejecucion, asi que no necesitan el modelo mas caro. Se dejan
# vacios por defecto (= el que tenga configurado la CLI) para no romper nada; pon
# PLAN_MODEL=sonnet y REVIEW_MODEL=sonnet en el .env cuando quieras bajar el coste.
#
# No fijes un modelo aqui sin medir antes: sin usage_log no sabes si el cambio ahorra
# o si degrada la calidad del plan, que es lo que luego se compara contra el diff.
PLAN_MODEL = os.environ.get("PLAN_MODEL", "").strip()
EXEC_MODEL = os.environ.get("EXEC_MODEL", "").strip()
REVIEW_MODEL = os.environ.get("REVIEW_MODEL", "").strip()


def _proyecto(directory_path: str) -> str:
    """
    Proyecto al que se imputa el gasto de una tarea: el nombre de su carpeta. Es el
    mismo nombre que usa el chat cuando el proyecto tiene carpeta vinculada
    (project_map.get_project_label).
    """
    return project_directory_map.label_for_directory(directory_path)


def _flags_modelo(modelo: str) -> list[str]:
    """['--model', X] si hay modelo configurado, o [] para usar el de la CLI."""
    return ["--model", modelo] if modelo else []

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# dev_log — persists Level-2 execution summaries (self-contained, no foreign deps)
# ---------------------------------------------------------------------------


def _dev_log_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    # WAL: sin esto un escritor bloquea la base entera. Contra este fichero escriben
    # el hilo del bot, el de uvicorn (internal_api) y los workers de asyncio.to_thread.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
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


# Comandos que la ejecucion puede lanzar sin aprobacion. `--permission-mode acceptEdits`
# solo aprueba ediciones de archivos: sin esta lista, en headless no hay nadie que apruebe
# un `git commit` o un `pytest` y se rechazan todos (todas las tareas acababan en el commit
# de fallback). Lista cerrada a proposito: nada de push, checkout, reset, rm ni Bash libre.
_EXEC_ALLOWED_TOOLS = ",".join([
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(pytest:*)",
    "Bash(python -m pytest:*)",
])


def execute_task(task_content: str, directory_path: str, origin: str | None = None) -> dict:
    """
    Invokes Claude Code headless with task_content as the prompt, working inside directory_path.
    Returns a dict with: result, cost_usd, session_id, error.
    `origin` (normally the branch name) is only used to label the usage_log entry.
    Never raises — all failures are captured in the 'error' key.
    """
    try:
        proc = subprocess.run(
            [
                "claude",
                "-p", task_content,
                "--output-format", "json",
                "--permission-mode", "acceptEdits",
                "--allowedTools", _EXEC_ALLOWED_TOOLS,
                "--add-dir", directory_path,
                *_flags_modelo(EXEC_MODEL),
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

    usage_log.record("claude_code", _proyecto(directory_path), "exec", data, origin=origin)

    return {
        "result": data.get("result"),
        "cost_usd": _extraer_coste(data),
        "usage": _extraer_uso(data),
        "session_id": data.get("session_id"),
        "git_diff": get_git_diff(directory_path),
        "error": None,
    }


def _extraer_coste(data: dict) -> float | None:
    """
    Coste en USD de esta invocacion de claude -p.

    La CLI emite `total_cost_usd` ("total" = de esta sesion, y cada `claude -p` es una
    sesion nueva; con varios turnos internos los suma). Versiones antiguas usaban
    `cost_usd`, asi que se acepta como alternativa.

    Si no aparece ninguno se avisa por log en vez de devolver None en silencio: la CLI
    no esta pinneada en el Dockerfile y un rebuild puede volver a renombrar el campo.
    """
    for clave in ("total_cost_usd", "cost_usd"):
        valor = data.get(clave)
        if valor is not None:
            return valor

    logger.warning(
        "claude -p no devolvio ningun campo de coste conocido. Claves recibidas: %s. "
        "Probablemente la CLI cambio de formato; revisa _extraer_coste().",
        sorted(data.keys()),
    )
    return None


def _extraer_uso(data: dict) -> dict | None:
    """
    Tokens consumidos por esta invocacion, aplanados a lo que interesa registrar.
    Devuelve None si la CLI no informa de uso.
    """
    uso = data.get("usage")
    if not isinstance(uso, dict):
        return None

    modelos = data.get("modelUsage")
    modelo = next(iter(modelos), None) if isinstance(modelos, dict) else None

    return {
        "model": modelo,
        "input_tokens": uso.get("input_tokens"),
        "output_tokens": uso.get("output_tokens"),
        "cache_read_input_tokens": uso.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": uso.get("cache_creation_input_tokens"),
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


def _get_plan(task_content: str, directory_path: str, origin: str | None = None) -> str:
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
                *_flags_modelo(PLAN_MODEL),
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

    usage_log.record("claude_code", _proyecto(directory_path), "plan", data, origin=origin)

    return (data.get("result") or "").strip() or "(plan vacío)"


REVIEW_TIMEOUT_SECONDS = 240

_REVIEW_PROMPT_TEMPLATE = """Use the `reviewer` subagent to audit the work that was just \
completed on branch `{branch}`, comparing the real diff against the plan declared beforehand.

Declared plan:
{plan}

Original task:
{task_content}

The changes are the diff between `{original_branch}` and `{branch}` \
(`git diff {original_branch}..{branch}`).

Return the reviewer's report verbatim. Do not fix anything, do not add your own commentary, \
and do not approve or reject the task — that decision belongs to a human."""


def _run_reviewer(
    task_content: str,
    plan: str,
    branch_name: str,
    original_branch: str,
    directory_path: str,
) -> str:
    """
    Read-only review pass: hands the declared plan and the resulting diff to the `reviewer`
    subagent, which looks for scope creep, unrequested production rewrites and protected
    files touched.

    Detection, not prevention — this runs after the fact. It never blocks the flow and never
    raises: any failure comes back as a note in the returned string, because a review that
    could not run must be visible rather than silently absent.

    Disabled by setting REVIEWER_ENABLED=false (it costs a third claude -p call per task).
    """
    if os.environ.get("REVIEWER_ENABLED", "true").strip().lower() in {"0", "false", "no"}:
        return "(revisión desactivada por configuración: REVIEWER_ENABLED)"

    prompt = _REVIEW_PROMPT_TEMPLATE.format(
        branch=branch_name,
        plan=plan,
        task_content=task_content,
        original_branch=original_branch,
    )

    try:
        proc = subprocess.run(
            [
                "claude",
                "-p", prompt,
                "--output-format", "json",
                "--disallowedTools", "Edit,Write",
                "--add-dir", directory_path,
                *_flags_modelo(REVIEW_MODEL),
            ],
            cwd=directory_path,
            capture_output=True,
            text=True,
            timeout=REVIEW_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.warning("La revisión posterior superó %ds", REVIEW_TIMEOUT_SECONDS)
        return f"(la revisión no terminó en {REVIEW_TIMEOUT_SECONDS}s — revisa el diff a mano)"
    except Exception:
        logger.exception("Fallo al lanzar la revisión posterior")
        return "(no se pudo ejecutar la revisión — revisa el diff a mano)"

    if proc.returncode != 0:
        logger.warning("Revisión: claude -p exited %d", proc.returncode)
        return "(la revisión falló — revisa el diff a mano)"

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning("Revisión: salida no-JSON de claude -p")
        return "(la revisión devolvió una salida ilegible — revisa el diff a mano)"

    usage_log.record("claude_code", _proyecto(directory_path), "review", data, origin=branch_name)

    return (data.get("result") or "").strip() or "(la revisión no devolvió nada)"


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


_EXEC_PROMPT_TEMPLATE = """Follow the `ejecutar-tarea-dev` skill for this task.

Task:
{task_content}

Plan declared before execution. Stay within it; if it falls short, stop and report \
instead of widening the scope on your own:
{plan}
"""


def _build_exec_prompt(task_content: str, plan: str) -> str:
    """
    Prompt for the execution step. Names the skill explicitly (Claude Code did not load it
    from the description alone) and includes the declared plan, which the execution step
    never saw before even though CLAUDE.md, the skill and the reviewer all rely on it.
    """
    return _EXEC_PROMPT_TEMPLATE.format(task_content=task_content, plan=plan) + _COMMIT_INSTRUCTIONS


def _fallback_commit_message(branch_name: str) -> str:
    """
    Generic English commit subject for the fallback commit: "<prefix>: apply changes
    from automated dev task", keeping the branch prefix (feat/fix/chore).

    It does NOT use the branch slug: the slug comes from the task text, which is
    written in Spanish, so it produced subjects like "anadir docstring funcion ...".
    """
    prefix = branch_name.split("/", 1)[0] if "/" in branch_name else "chore"
    return f"{prefix}: apply changes from automated dev task"


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


def _checkout_safe(branch: str, directory_path: str) -> str | None:
    """
    Returns to branch. If the repo is dirty (uncommitted changes remain after a failed
    auto-commit), the changes are stashed first so nothing is lost.

    Returns the stash label if one was created, None otherwise.
    """
    rc, porcelain, _ = _git(["status", "--porcelain"], directory_path)
    is_dirty = rc == 0 and bool(porcelain)

    etiqueta_stash = None
    if is_dirty:
        # NO se descarta con `checkout -f`: el git_diff del resultado esta truncado a
        # 5000 caracteres, asi que un descarte pierde trabajo de verdad. Se guarda en
        # un stash, que siempre se puede recuperar con `git stash list`.
        etiqueta_stash = f"foncu-auto: cambios sin commitear al salir de {branch}"
        rc, _, err = _git(["stash", "push", "-u", "-m", etiqueta_stash], directory_path)
        if rc != 0:
            logger.critical(
                "No se pudo hacer stash en '%s' (%s). NO se cambia de rama: es preferible "
                "dejar el repo donde esta a destruir los cambios.",
                branch, err,
            )
            return None
        logger.warning("Cambios sin commitear guardados en stash: %s", etiqueta_stash)

    rc, _, err = _git(["checkout", branch], directory_path)
    if rc != 0:
        logger.critical("No se pudo volver a la rama '%s': %s", branch, err)

    return etiqueta_stash


def execute_task_on_branch(task_content: str, directory_path: str) -> dict:
    """
    Level-2 flow: creates a typed branch (feat/fix/chore), runs the task there asking
    claude -p to commit its own changes with a proper English message, then always
    returns to the original branch. Never merges to main.

    Extra keys in the returned dict:
      branch            — name of the created branch
      plan              — plan described by a read-only claude -p call before any change,
                           made for the reviewer subagent to compare against the real diff
      review            — report from the `reviewer` subagent comparing that plan against
                           the real diff (scope creep, unrequested rewrites, protected files)
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

    plan = _get_plan(task_content, directory_path, origin=branch_name)

    enriched_prompt = _build_exec_prompt(task_content, plan)

    result: dict = {}
    try:
        result = execute_task(enriched_prompt, directory_path, origin=branch_name)
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
                    # CLAUDE.md: "la tarea se reporta como fallida con el motivo, nunca
                    # como hecho a medias". Si parte del trabajo no esta commiteada, no
                    # hay nada que dar por bueno: el estado del repo no es el que se
                    # reporta y no se puede revisar ni revertir con fiabilidad.
                    result["error"] = (
                        "La tarea NO se puede dar por completada: quedaron cambios sin "
                        f"commitear y el commit automatico fallo ({commit_err}). Revisa el "
                        f"estado de la rama '{branch_name}' a mano antes de seguir."
                    )

            if result.get("auto_commit_error"):
                # Revisar un arbol a medio commitear daria un informe sobre un estado
                # que no es el que quedara. Mejor decir que no se reviso.
                result["review"] = (
                    "(sin revisar: el commit automatico fallo, el diff no es fiable)"
                )
            else:
                # Compara el diff real contra el plan declarado. Va despues del commit
                # de fallback para que el diff este completo.
                result["review"] = _run_reviewer(
                    task_content, plan, branch_name, original_branch, directory_path
                )

            _save_dev_log(task_content, branch_name, result.get("result") or "", plan=plan)
    finally:
        etiqueta_stash = _checkout_safe(original_branch, directory_path)
        if etiqueta_stash and isinstance(result, dict):
            result["stash"] = etiqueta_stash
            result["stash_aviso"] = (
                "El commit automatico fallo y quedaban cambios sin guardar. Estan en un "
                f"stash de la rama '{branch_name}': recuperalos con `git stash list` y "
                "`git stash apply`."
            )

    return result
