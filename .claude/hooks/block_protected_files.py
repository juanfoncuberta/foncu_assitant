#!/usr/bin/env python3
"""
PreToolUse hook: bloquea escrituras sobre archivos protegidos.

Claude Code invoca este script antes de cada Edit/Write y le pasa por stdin un
JSON con la tool y sus argumentos. Si el script sale con codigo 2, la llamada se
bloquea y lo escrito en stderr se le devuelve a Claude como motivo.

La lista debe coincidir con la seccion "Archivos protegidos" de CLAUDE.md.
"""
import json
import os
import sys

PROTECTED_FILES = {
    ".env",
    "docker-compose.yml",
    "Dockerfile",
    "deploy.sh",
}
PROTECTED_DIRS = (
    ".claude/",
    "migrations/",
)


def is_protected(path: str) -> str | None:
    """Devuelve el motivo si la ruta esta protegida, o None si se puede escribir."""
    if not path:
        return None

    rel = os.path.relpath(os.path.abspath(path), os.getcwd())
    # Normaliza separadores para que la comparacion funcione en cualquier plataforma.
    rel = rel.replace(os.sep, "/")

    if rel.startswith("../"):
        return f"'{rel}' esta fuera del repositorio."

    basename = os.path.basename(rel)
    if basename in PROTECTED_FILES:
        return f"'{basename}' es un archivo protegido."

    for directory in PROTECTED_DIRS:
        if rel.startswith(directory):
            return f"'{rel}' esta bajo '{directory}', que es un directorio protegido."

    if basename.endswith(".sql") or "migration" in rel.lower():
        return f"'{rel}' parece una migracion de base de datos."

    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Sin payload legible no hay nada que juzgar: no bloqueamos.
        return 0

    tool_input = payload.get("tool_input") or {}
    # Edit y Write usan 'file_path'; algunas variantes usan 'path' o 'notebook_path'.
    path = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("notebook_path")
        or ""
    )

    reason = is_protected(path)
    if reason:
        print(
            f"BLOQUEADO: {reason}\n"
            "Esta ruta requiere aprobacion humana explicita (ver 'Archivos protegidos' "
            "en CLAUDE.md). No busques una via alternativa: reportalo en tu resumen final.",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
