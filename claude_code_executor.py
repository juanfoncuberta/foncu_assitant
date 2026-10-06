"""
Executes development tasks via Claude Code CLI in headless (non-interactive) mode.
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import project_directory_map
import usage_log

DB_PATH = os.environ.get("DB_PATH", "assistant.db")

TIMEOUT_SECONDS = 600  # 10 minutes

# Tope del diff que se devuelve en `git_diff`. Ese diff es SOLO para ensenarselo al
# usuario en Telegram: el reviewer nunca lo usa, revisa el diff completo que le vuelca
# _run_reviewer a disco. Si algun dia se le pasa este recorte al reviewer, puede dar
# por bueno un cambio que no ha visto (hallazgo A3 de la auditoria del 28/09).
DISPLAY_DIFF_MAX_CHARS = 5000

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
    # Columnas anadidas despues de crear la tabla. Las filas antiguas se quedan con el
    # valor por defecto: sin carpeta ni rama base no se pueden volver a revisar.
    for columna, definicion in (
        ("plan", "TEXT NOT NULL DEFAULT ''"),
        ("base_branch", "TEXT NOT NULL DEFAULT ''"),
        ("directory", "TEXT NOT NULL DEFAULT ''"),
        # completa / no_revisada / desactivada; '' = anterior a guardar el estado.
        ("review_status", "TEXT NOT NULL DEFAULT ''"),
        # Para calibrar el tiempo limite de la revision con datos reales.
        ("review_seconds", "REAL"),
        ("review_lines", "INTEGER"),
    ):
        if columna not in existing_cols:
            conn.execute(f"ALTER TABLE dev_log ADD COLUMN {columna} {definicion}")
    conn.commit()
    return conn


def _save_dev_log(
    task_content: str,
    branch: str,
    summary: str,
    plan: str = "",
    base_branch: str = "",
    directory: str = "",
    revision: dict | None = None,
) -> None:
    revision = revision or {}
    with _dev_log_conn() as conn:
        conn.execute(
            """
            INSERT INTO dev_log (task_content, branch, summary, plan, base_branch, directory,
                                 review_status, review_seconds, review_lines)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_content, branch, summary, plan, base_branch, directory,
             revision.get("estado", ""), revision.get("segundos"), revision.get("lineas")),
        )


def _actualizar_revision_dev_log(branch: str, revision: dict) -> None:
    """Guarda el resultado de una nueva revision en la entrada mas reciente de la rama."""
    with _dev_log_conn() as conn:
        conn.execute(
            """
            UPDATE dev_log SET review_status = ?, review_seconds = ?, review_lines = ?
            WHERE id = (SELECT MAX(id) FROM dev_log WHERE branch = ?)
            """,
            (revision["estado"], revision.get("segundos"), revision.get("lineas"), branch),
        )


def get_dev_log_entry(branch: str) -> dict | None:
    """Entrada mas reciente de dev_log para una rama, o None si no existe."""
    with _dev_log_conn() as conn:
        fila = conn.execute(
            """
            SELECT task_content, branch, plan, base_branch, directory, review_status, created_at
            FROM dev_log WHERE branch = ? ORDER BY id DESC LIMIT 1
            """,
            (branch,),
        ).fetchone()
    if fila is None:
        return None
    claves = ("task_content", "branch", "plan", "base_branch", "directory",
              "review_status", "created_at")
    return dict(zip(claves, fila))


def get_unreviewed_dev_log_entries() -> list[dict]:
    """
    Tareas de desarrollo cuya ultima revision no es completa. Excluye las anteriores a
    guardar carpeta y rama base: no se pueden volver a revisar ni saber si se integraron.
    """
    with _dev_log_conn() as conn:
        filas = conn.execute(
            """
            SELECT d.task_content, d.branch, d.base_branch, d.directory, d.review_status,
                   d.created_at
            FROM dev_log d
            WHERE d.id = (SELECT MAX(id) FROM dev_log WHERE branch = d.branch)
              AND d.review_status != 'completa'
              AND d.directory != '' AND d.base_branch != ''
            ORDER BY d.id DESC
            """
        ).fetchall()
    claves = ("task_content", "branch", "base_branch", "directory", "review_status", "created_at")
    return [dict(zip(claves, f)) for f in filas]


def estado_rama(directory_path: str, branch: str, base_branch: str) -> str:
    """
    'pendiente' si la rama existe y no esta integrada en la base, 'integrada' si ya lo
    esta, 'no_existe' si se borro, o 'desconocido' si git falla.
    """
    try:
        rc, _, _ = _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                        directory_path)
        if rc != 0:
            return "no_existe"
        rc, _, _ = _git(["merge-base", "--is-ancestor", branch, base_branch], directory_path)
    except Exception:
        logger.exception("No se pudo consultar el estado de la rama %s", branch)
        return "desconocido"
    if rc == 0:
        return "integrada"
    return "pendiente" if rc == 1 else "desconocido"


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
    """
    Returns the combined staged+unstaged diff after task execution, truncated to
    DISPLAY_DIFF_MAX_CHARS. Display only: never hand this to the reviewer.
    """
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
        return _recortar_para_mostrar(diff)
    except Exception:
        return "(no se pudo obtener el diff)"


def _recortar_para_mostrar(diff: str) -> str:
    """Recorta un diff para mostrarlo al usuario. No sirve para revisar."""
    if len(diff) <= DISPLAY_DIFF_MAX_CHARS:
        return diff
    return diff[:DISPLAY_DIFF_MAX_CHARS] + "…(truncado)"


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


# Tiempo limite de la revision: una base, mas un poco por cada linea cambiada, con un
# tope. Valores provisionales: cada revision guarda en dev_log cuanto tardo y cuantas
# lineas tenia (review_seconds, review_lines), para recalibrarlos con datos reales.
# Ojo al subirlos: mientras dura una tarea el bot no atiende otros mensajes.
REVIEW_TIMEOUT_SECONDS = 240
REVIEW_TIMEOUT_MAX_SECONDS = 600
REVIEW_SECONDS_PER_CHANGED_LINE = 0.5

ESTADO_COMPLETA = "completa"
ESTADO_INCOMPLETA = "incompleta"
ESTADO_SIN_REVISAR = "sin_revisar"
ESTADO_DESACTIVADA = "desactivada"
ESTADO_NO_REVISADA = "no_revisada"


def _timeout_revision(lineas_cambiadas: int) -> int:
    return int(min(
        REVIEW_TIMEOUT_MAX_SECONDS,
        REVIEW_TIMEOUT_SECONDS + lineas_cambiadas * REVIEW_SECONDS_PER_CHANGED_LINE,
    ))

# Por encima de estas lineas cambiadas (sumando anadidas y borradas) se le pide al
# reviewer que trabaje archivo a archivo, cerrando los hallazgos de cada uno antes de
# leer el siguiente, en vez de intentar abarcar todo el diff de una vez.
REVIEW_PER_FILE_THRESHOLD_LINES = 400

_REVIEW_PROMPT_TEMPLATE = """Use the `reviewer` subagent to audit the work that was just \
completed on branch `{branch}`, comparing the real diff against the plan declared beforehand.

Declared plan:
{plan}

Original task:
{task_content}

The changes are the diff between `{original_branch}` and `{branch}`. The full diff has \
already been written to disk, one file per changed path, so the review does not depend on \
Bash and nothing is truncated. Pass this list to the reviewer verbatim: it is the complete \
set of changes ({n_files} files, +{added} -{deleted} lines).

{manifest}
{per_file_mode}
The reviewer must read every diff file in full with `Read` (paging with offset/limit when \
a file is longer than one read) and must end its report with the `COBERTURA:` line and the \
list of reviewed files, as its instructions require. A file it has not read in full does not \
count as reviewed.

Return the reviewer's report verbatim. Do not fix anything, do not add your own commentary, \
and do not approve or reject the task — that decision belongs to a human."""

_PER_FILE_MODE = """
This diff is large: review it file by file. Finish each file (write down its findings) \
before reading the next one, and never try to load every diff at once.
"""

_COBERTURA_RE = re.compile(
    r"COBERTURA:\s*revisados\s+(\d+)\s+de\s+(\d+)\s+archivos", re.IGNORECASE
)


def _manifiesto_diff(original_branch: str, branch: str, directory_path: str) -> list[dict] | None:
    """
    Lista completa de archivos que cambian entre las dos ramas, con sus lineas anadidas
    y borradas. La calcula el executor, no el modelo, para tener una referencia fija
    contra la que comprobar que el reviewer lo ha mirado todo.

    Devuelve None si git falla o su salida no se entiende: es preferible no revisar que
    revisar contra una lista incompleta y dar a entender que se reviso todo.
    """
    try:
        rc, salida, _ = _git(
            ["diff", "--numstat", "-z", "--no-renames", f"{original_branch}...{branch}"],
            directory_path,
        )
    except Exception:
        logger.exception("No se pudo listar los archivos del diff para el reviewer")
        return None
    if rc != 0:
        return None

    archivos = []
    for registro in salida.split("\0"):
        if not registro:
            continue
        partes = registro.split("\t", 2)
        if len(partes) != 3 or not partes[2]:
            logger.error("Salida de git diff --numstat inesperada: %r", registro[:200])
            return None
        anadidas, borradas, ruta = partes
        if anadidas == "-" and borradas == "-":
            archivos.append({"path": ruta, "added": 0, "deleted": 0, "binary": True})
        elif anadidas.isdigit() and borradas.isdigit():
            archivos.append({
                "path": ruta, "added": int(anadidas), "deleted": int(borradas), "binary": False,
            })
        else:
            logger.error("Salida de git diff --numstat inesperada: %r", registro[:200])
            return None
    return archivos


def _commits_propios(original_branch: str, branch: str, directory_path: str) -> int | None:
    """Commits de `branch` que no estan en `original_branch`, o None si git falla."""
    try:
        rc, salida, _ = _git(["rev-list", "--count", f"{original_branch}..{branch}"],
                             directory_path)
    except Exception:
        logger.exception("No se pudo contar los commits de %s", branch)
        return None
    if rc != 0 or not salida.isdigit():
        return None
    return int(salida)


def _volcar_diffs(
    manifiesto: list[dict], original_branch: str, branch: str, directory_path: str, carpeta: str
) -> bool:
    """
    Escribe el diff COMPLETO de cada archivo del manifiesto en `carpeta`, uno por
    archivo, y anota su ruta en `diff_file`. Asi el reviewer lo lee con Read (que pagina
    sin limite) en vez de depender de Bash, que en headless no tiene permiso y cuya
    salida se recorta. Devuelve False si algun diff no se pudo obtener.
    """
    for i, archivo in enumerate(manifiesto, 1):
        try:
            # --literal-pathspecs: la ruta se usa tal cual, sin interpretar comodines.
            rc, diff, _ = _git(
                ["--literal-pathspecs", "diff", "--no-renames",
                 f"{original_branch}...{branch}", "--", archivo["path"]],
                directory_path,
                strip=False,  # tal cual: sin perder espacios finales de la ultima linea
            )
        except Exception:
            logger.exception("No se pudo obtener el diff de %s para el reviewer", archivo["path"])
            return False
        if rc != 0:
            return False
        destino = os.path.join(carpeta, f"{i:03d}.diff")
        try:
            with open(destino, "w", encoding="utf-8") as f:
                f.write(diff)
        except OSError:
            logger.exception("No se pudo escribir el diff de %s para el reviewer", archivo["path"])
            return False
        archivo["diff_file"] = destino
        archivo["diff_lines"] = diff.count("\n")
    return True


def _texto_manifiesto(manifiesto: list[dict]) -> str:
    if not manifiesto:
        return "(no changes between the two branches)"
    lineas = []
    for a in manifiesto:
        cambios = "binary" if a["binary"] else f"+{a['added']} -{a['deleted']}"
        lineas.append(
            f"- {a['path']} ({cambios}, {a['diff_lines']} diff lines): {a['diff_file']}"
        )
    return "\n".join(lineas)


def _rutas_de_bloque(texto: str, titulo: str) -> list[str]:
    """
    Rutas listadas como `- ruta` justo debajo de la linea `titulo` (p. ej.
    "Archivos revisados:"), hasta la primera linea que no sea de la lista.
    """
    rutas = []
    dentro = False
    buscado = _normalizar_cabecera(titulo)
    for linea in texto.splitlines():
        limpia = linea.strip()
        if not dentro:
            dentro = _normalizar_cabecera(limpia) == buscado
            continue
        if limpia[:2] in ("- ", "* ", "+ "):
            rutas.append(limpia[2:].strip().strip("`"))
        elif limpia:
            break
    return rutas


def _normalizar_cabecera(linea: str) -> str:
    """'**Archivos revisados** (5):' -> 'archivos revisados'. Tolera el formato markdown."""
    sin_parentesis = re.sub(r"\(.*?\)", "", linea)
    return re.sub(r"[*_:#\s]+", " ", sin_parentesis).strip().lower()


def _verificar_cobertura(informe: str, manifiesto: list[dict]) -> dict:
    """
    Contrasta lo que el reviewer dice haber revisado con la lista real de git.

    No puede comprobar que el reviewer leyo cada archivo (eso solo lo sabe el modelo),
    pero si que declara haberlos revisado todos y que su lista "Archivos revisados" es
    exactamente la de git (comparacion exacta de rutas, no subcadenas: `main.py` no
    cuenta como nombrado porque aparezca `tests/test_main.py`). Si algo no cuadra, la
    revision se marca como incompleta en vez de darla por buena.
    """
    total = len(manifiesto)
    esperadas = [a["path"] for a in manifiesto]
    coincidencias = list(_COBERTURA_RE.finditer(informe))
    declarados = de_total = None
    revisadas: set[str] = set()
    sin_revisar_declaradas: list[str] = []
    if coincidencias:
        ultima = coincidencias[-1]
        declarados, de_total = int(ultima.group(1)), int(ultima.group(2))
        bloque = informe[ultima.start():]
        revisadas = set(_rutas_de_bloque(bloque, "Archivos revisados:"))
        sin_revisar_declaradas = _rutas_de_bloque(bloque, "Archivos sin revisar:")

    sin_revisar = [r for r in esperadas if r not in revisadas or r in sin_revisar_declaradas]

    if total == 0:
        completa = True
    else:
        completa = declarados == total and de_total == total and not sin_revisar

    return {
        "total": total,
        "declarados": declarados,
        "sin_revisar": sin_revisar,
        "completa": completa,
    }


def _cabecera_cobertura(cobertura: dict) -> str:
    """Primera linea del informe: cuanto del diff cubre la revision."""
    total = cobertura["total"]
    if cobertura["completa"]:
        return (
            f"Cobertura de la revisión: revisados {total} de {total} archivos "
            "(lo declara el reviewer y su lista coincide con la de git)."
        )
    declarados = cobertura["declarados"]
    partes = [
        f"⚠️ REVISIÓN INCOMPLETA: el reviewer declara "
        + (f"{declarados} de {total} archivos" if declarados is not None
           else f"una cobertura que no se puede leer (cambiaron {total} archivos)")
        + "."
    ]
    if cobertura["sin_revisar"]:
        partes.append(
            "Sin revisar o sin listar como revisados: "
            + ", ".join(cobertura["sin_revisar"]) + "."
        )
    partes.append("Su veredicto no cubre todo el cambio: revisa el diff a mano.")
    return " ".join(partes)


def _run_reviewer(
    task_content: str,
    plan: str,
    branch_name: str,
    original_branch: str,
    directory_path: str,
) -> str:
    """Una sola pasada de revision; devuelve solo el texto. Ver _revision."""
    return _revision(task_content, plan, branch_name, original_branch, directory_path)["texto"]


def _revision(
    task_content: str,
    plan: str,
    branch_name: str,
    original_branch: str,
    directory_path: str,
    *,
    por_archivo: bool = False,
    timeout: int | None = None,
) -> dict:
    """
    Read-only review pass: hands the declared plan and the resulting diff to the `reviewer`
    subagent, which looks for scope creep, unrequested production rewrites and protected
    files touched.

    The reviewer gets the FULL diff, written to a temp folder one file per changed path
    (never the truncated `git_diff` shown to the user), plus the list of files from
    `git diff --numstat`. Its report must declare how many of those files it reviewed;
    the first line of the text states that coverage, and flags the review as incomplete
    when the reviewer does not account for every file (finding A3).

    The diff is `base...branch` (three dots): only what the branch adds since it split
    from the base, even if the base moved on afterwards (re-reviews days later).

    Never raises. Returns {texto, estado, segundos, lineas}; estado is one of
    completa / incompleta / sin_revisar / desactivada.

    Disabled by setting REVIEWER_ENABLED=false (it costs a third claude -p call per task).
    """
    resultado = {"texto": "", "estado": ESTADO_SIN_REVISAR, "segundos": None, "lineas": None}
    if os.environ.get("REVIEWER_ENABLED", "true").strip().lower() in {"0", "false", "no"}:
        resultado.update(
            texto="❌ NO REVISADA: revisión desactivada por configuración (REVIEWER_ENABLED).",
            estado=ESTADO_DESACTIVADA,
        )
        return resultado

    sin_diff = (
        "(sin revisar: no se pudo preparar el diff completo para el reviewer — "
        "revisa el diff a mano)"
    )
    if original_branch == "HEAD":
        # "HEAD" no es una base: estando en la rama, HEAD...rama sale vacio y pasaria
        # por una revision completa de 0 archivos.
        resultado["texto"] = (
            "(sin revisar: no hay rama base contra la que comparar (detached HEAD) — "
            "revisa la rama a mano)"
        )
        return resultado
    try:
        carpeta = tempfile.mkdtemp(prefix="foncu-review-")
    except Exception:
        logger.exception("No se pudo crear la carpeta temporal de la revisión")
        resultado["texto"] = sin_diff
        return resultado
    try:
        manifiesto = _manifiesto_diff(original_branch, branch_name, directory_path)
        if manifiesto is None or not _volcar_diffs(
            manifiesto, original_branch, branch_name, directory_path, carpeta
        ):
            resultado["texto"] = sin_diff
            return resultado
        if not manifiesto and _commits_propios(original_branch, branch_name, directory_path) != 0:
            # Diff vacio pero la rama tiene commits (o no se puede saber): "0 de 0
            # archivos" seria un visto bueno sobre algo que no se ha visto. Pasa, por
            # ejemplo, si la base no es la rama real de la que salio la tarea.
            resultado["texto"] = (
                f"(sin revisar: la rama {branch_name} tiene commits propios pero su diff "
                f"contra {original_branch} sale vacío — revisa la rama a mano)"
            )
            return resultado
        lineas = sum(a["added"] + a["deleted"] for a in manifiesto)
        resultado["lineas"] = lineas
        inicio = time.monotonic()
        informe, revisado = _lanzar_reviewer(
            task_content, plan, branch_name, original_branch, directory_path,
            manifiesto, carpeta,
            por_archivo=por_archivo,
            timeout=timeout if timeout is not None else _timeout_revision(lineas),
        )
        resultado["segundos"] = round(time.monotonic() - inicio, 1)
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)

    if not revisado:
        # La revision no llego a hacerse: el mensaje ya lo dice, no hay cobertura que medir.
        resultado["texto"] = informe
        return resultado
    cobertura = _verificar_cobertura(informe, manifiesto)
    if not cobertura["completa"]:
        logger.warning("Revisión incompleta en %s: %s", branch_name, cobertura)
    resultado["texto"] = _cabecera_cobertura(cobertura) + "\n\n" + informe
    resultado["estado"] = ESTADO_COMPLETA if cobertura["completa"] else ESTADO_INCOMPLETA
    return resultado


def _revisar_con_reintento(
    task_content: str,
    plan: str,
    branch_name: str,
    original_branch: str,
    directory_path: str,
) -> dict:
    """
    La revision es obligatoria: si falla o queda incompleta se repite una vez, archivo a
    archivo y con el tiempo maximo. Si tampoco sale, la tarea queda como NO REVISADA,
    con un aviso que tiene que ir delante de todo lo demas.

    Devuelve {texto, estado, segundos, lineas}; estado es completa, desactivada o
    no_revisada. `segundos` suma los dos intentos.
    """
    primera = _revision(task_content, plan, branch_name, original_branch, directory_path)
    if primera["estado"] in (ESTADO_COMPLETA, ESTADO_DESACTIVADA):
        return primera

    logger.warning(
        "Revisión de %s: primer intento %s; se repite archivo a archivo",
        branch_name, primera["estado"],
    )
    segunda = _revision(
        task_content, plan, branch_name, original_branch, directory_path,
        por_archivo=True, timeout=REVIEW_TIMEOUT_MAX_SECONDS,
    )
    segundos = [r["segundos"] for r in (primera, segunda) if r["segundos"] is not None]
    segunda["segundos"] = round(sum(segundos), 1) if segundos else None
    if segunda["lineas"] is None:
        segunda["lineas"] = primera["lineas"]

    if segunda["estado"] == ESTADO_COMPLETA:
        segunda["texto"] += (
            "\n\n(Revisión hecha en el segundo intento, archivo a archivo: el primero "
            f"fue {primera['estado'].replace('_', ' ')}.)"
        )
        return segunda

    def _motivo(r: dict) -> str:
        return (r["texto"].splitlines() or ["(sin texto)"])[0]

    informes = []
    for nombre, r in (("primer", primera), ("segundo", segunda)):
        # Solo una revision incompleta trae informe: sus hallazgos parciales siguen
        # valiendo y no se pueden perder por que el otro intento falle.
        if r["estado"] == ESTADO_INCOMPLETA:
            informes.append(f"Informe parcial del {nombre} intento:\n{r['texto']}")
    segunda["estado"] = ESTADO_NO_REVISADA
    segunda["texto"] = (
        f"❌ NO REVISADA: la rama {branch_name} no tiene una revisión completa tras dos "
        "intentos. No la integres así: pide que se revise otra vez (revisar_rama_dev) o "
        "revísala tú.\n"
        f"- Primer intento: {_motivo(primera)}\n"
        f"- Segundo intento: {_motivo(segunda)}"
        + "".join(f"\n\n{i}" for i in informes)
    )
    return segunda


def revisar_rama(entrada: dict, directory_path: str) -> dict:
    """
    Vuelve a revisar una rama ya registrada en dev_log (tool `revisar_rama_dev`). No
    cambia de rama ni toca el arbol de trabajo: solo lee el diff base...rama. La carpeta
    llega ya revalidada por quien llama.
    """
    branch = entrada["branch"]
    base = entrada["base_branch"]
    estado = estado_rama(directory_path, branch, base)
    if estado == "no_existe":
        return {"error": f"La rama {branch} ya no existe en {directory_path}."}
    if estado == "integrada":
        # Integrada, `base...rama` sale vacio: la revision veria 0 archivos y la daria
        # por buena. Se dice tal cual y no se toca dev_log.
        return {
            "error": (
                f"La rama {branch} ya está integrada en {base}: no se puede revisar desde "
                "aquí porque su diff contra la base ya sale vacío. Revisa el commit a mano."
            ),
            "revision_valida": False,
        }
    revision = _revisar_con_reintento(
        entrada["task_content"], entrada.get("plan") or "(sin plan registrado)",
        branch, base, directory_path,
    )
    _actualizar_revision_dev_log(branch, revision)
    return {
        "branch": branch,
        "review": revision["texto"],
        "revision_estado": revision["estado"],
        "revision_valida": revision["estado"] == ESTADO_COMPLETA,
    }


def _lanzar_reviewer(
    task_content: str,
    plan: str,
    branch_name: str,
    original_branch: str,
    directory_path: str,
    manifiesto: list[dict],
    carpeta: str,
    *,
    por_archivo: bool = False,
    timeout: int = REVIEW_TIMEOUT_SECONDS,
) -> tuple[str, bool]:
    """
    Lanza el claude -p de revision. Devuelve (informe, True) si el reviewer contesto, o
    (motivo del fallo, False) si no llego a revisar nada.
    """
    lineas_cambiadas = sum(a["added"] + a["deleted"] for a in manifiesto)
    prompt = _REVIEW_PROMPT_TEMPLATE.format(
        branch=branch_name,
        plan=plan,
        task_content=task_content,
        original_branch=original_branch,
        n_files=len(manifiesto),
        added=sum(a["added"] for a in manifiesto),
        deleted=sum(a["deleted"] for a in manifiesto),
        manifest=_texto_manifiesto(manifiesto),
        per_file_mode=(
            _PER_FILE_MODE
            if por_archivo or lineas_cambiadas > REVIEW_PER_FILE_THRESHOLD_LINES
            else ""
        ),
    )

    try:
        proc = subprocess.run(
            [
                "claude",
                "-p", prompt,
                "--output-format", "json",
                "--disallowedTools", "Edit,Write",
                "--add-dir", directory_path,
                # Solo lectura: Edit/Write siguen prohibidos y esta carpeta solo tiene
                # los diffs volcados para esta revision.
                "--add-dir", carpeta,
                *_flags_modelo(REVIEW_MODEL),
            ],
            cwd=directory_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("La revisión posterior superó %ds", timeout)
        return (f"(la revisión no terminó en {timeout}s — revisa el diff a mano)", False)
    except Exception:
        logger.exception("Fallo al lanzar la revisión posterior")
        return ("(no se pudo ejecutar la revisión — revisa el diff a mano)", False)

    if proc.returncode != 0:
        logger.warning("Revisión: claude -p exited %d", proc.returncode)
        return ("(la revisión falló — revisa el diff a mano)", False)

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning("Revisión: salida no-JSON de claude -p")
        return ("(la revisión devolvió una salida ilegible — revisa el diff a mano)", False)

    usage_log.record("claude_code", _proyecto(directory_path), "review", data, origin=branch_name)

    informe = (data.get("result") or "").strip()
    if not informe:
        return "(la revisión no devolvió nada — revisa el diff a mano)", False
    return informe, True


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


def _git(args: list, cwd: str, timeout: int = 30, strip: bool = True) -> tuple:
    """
    Lanza git y devuelve (returncode, stdout, stderr). `strip=False` deja stdout tal
    cual: hace falta cuando el contenido importa byte a byte (el diff del reviewer).
    """
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    stdout = result.stdout.strip() if strip else result.stdout
    return result.returncode, stdout, result.stderr.strip()


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
        # DISPLAY_DIFF_MAX_CHARS caracteres, asi que un descarte pierde trabajo de verdad. Se guarda en
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
      revision_estado   — completa / no_revisada / desactivada
      revision_valida   — True only for a complete review; otherwise the task is NOT ready
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

    if original_branch == "HEAD":
        # Detached HEAD: no hay rama a la que volver ni base contra la que revisar
        # (`HEAD...rama` estando en la rama sale vacio y pasaria por revisado).
        return {
            "error": (
                "La carpeta está en detached HEAD (no hay ninguna rama activa). Cambia a "
                "una rama antes de lanzar la tarea."
            ),
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
            diff = _recortar_para_mostrar(branch_diff)
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
                revision = {
                    "texto": (
                        "❌ NO REVISADA: el commit automático falló y el diff no es "
                        "fiable — revisa la rama a mano."
                    ),
                    "estado": ESTADO_NO_REVISADA, "segundos": None, "lineas": None,
                }
            else:
                # Compara el diff real contra el plan declarado. Va despues del commit
                # de fallback para que el diff este completo.
                revision = _revisar_con_reintento(
                    task_content, plan, branch_name, original_branch, directory_path
                )
            result["review"] = revision["texto"]
            result["revision_estado"] = revision["estado"]
            # Solo una revision completa vale. Sin ella la tarea no esta lista para
            # integrar, aunque el commit exista en su rama.
            result["revision_valida"] = revision["estado"] == ESTADO_COMPLETA

            _save_dev_log(
                task_content, branch_name, result.get("result") or "", plan=plan,
                base_branch=original_branch, directory=directory_path, revision=revision,
            )
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
