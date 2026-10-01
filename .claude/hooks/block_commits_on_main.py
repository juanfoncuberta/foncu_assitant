#!/usr/bin/env python3
"""
PreToolUse hook: impide que Claude Code commitee, mergee o haga push sobre main.

Claude Code invoca este script antes de cada llamada a Bash y le pasa por stdin un
JSON con el comando y el directorio de trabajo ('cwd'). Si el script sale con
codigo 2, la llamada se bloquea y lo escrito en stderr se le devuelve a Claude.

Existe porque la regla "todo commit va en su propia rama" solo la aplicaba el
executor de ejecutar_tarea_dev, que crea la rama el mismo. En una sesion
interactiva no habia nada que la impusiera, y un commit acabo directo en main.
La regla escrita vive en la seccion "Ramas" de CLAUDE.md.

Solo afecta a Claude Code. Tu terminal y los subprocess del executor no pasan
por aqui, asi que mergear a main a mano sigue funcionando igual.
"""
import json
import re
import shlex
import subprocess
import sys

PROTECTED_BRANCHES = {"main", "master"}

# Subcomandos que crean commits o reescriben la rama en la que se ejecutan.
WRITE_SUBCOMMANDS = {"commit", "merge", "cherry-pick", "revert", "am", "rebase", "pull", "push"}

# Opciones globales de git que llevan un valor separado: `git -C ruta commit`.
_GLOBAL_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}

_SEPARADORES = re.compile(r"&&|\|\||;|\||\n")


def _git_invocations(command: str) -> list[list[str]]:
    """Trocea el comando por separadores de shell y devuelve los argv que empiezan por git."""
    invocaciones = []
    for trozo in _SEPARADORES.split(command):
        try:
            argv = shlex.split(trozo)
        except ValueError:
            argv = trozo.split()
        # Salta asignaciones de entorno delante del comando: `GIT_X=1 git commit`.
        while argv and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]):
            argv = argv[1:]
        if argv and argv[0] == "git":
            invocaciones.append(argv)
    return invocaciones


def _parse(argv: list[str]) -> tuple[str | None, str | None, list[str]]:
    """Devuelve (directorio de -C, subcomando, argumentos del subcomando)."""
    directorio = None
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in _GLOBAL_OPTS_WITH_VALUE:
            if arg == "-C" and i + 1 < len(argv):
                directorio = argv[i + 1]
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return directorio, arg, argv[i + 1:]
    return directorio, None, []


def _rama_actual(cwd: str) -> str | None:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def _push_apunta_a_protegida(args: list[str]) -> bool:
    """`git push origin main`, `git push origin HEAD:main`, `git push origin x:refs/heads/main`."""
    posicionales = [a for a in args if not a.startswith("-")]
    for refspec in posicionales[1:]:  # el primero es el remoto
        destino = refspec.lstrip("+").split(":")[-1]
        destino = destino.removeprefix("refs/heads/")
        if destino in PROTECTED_BRANCHES:
            return True
    return False


def motivo_de_bloqueo(command: str, cwd: str) -> str | None:
    """Devuelve el motivo si el comando escribe sobre una rama protegida, o None."""
    cambia_de_rama = False
    for argv in _git_invocations(command):
        directorio, sub, args = _parse(argv)
        if sub is None:
            continue

        if sub in {"checkout", "switch"}:
            # `git switch -c x && git commit` es justo lo correcto. No se puede saber
            # con certeza a que rama acabara apuntando HEAD, asi que a partir de aqui
            # se confia en el resto de la linea (el push explicito a main se sigue mirando).
            cambia_de_rama = True
            continue

        if sub == "push" and _push_apunta_a_protegida(args):
            return "el push apunta directamente a una rama protegida."

        if sub not in WRITE_SUBCOMMANDS or cambia_de_rama:
            continue

        rama = _rama_actual(directorio or cwd)
        if rama in PROTECTED_BRANCHES:
            return f"'git {sub}' sobre la rama '{rama}'."

    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    command = (payload.get("tool_input") or {}).get("command") or ""
    cwd = payload.get("cwd") or "."

    motivo = motivo_de_bloqueo(command, cwd)
    if motivo:
        print(
            f"BLOQUEADO: {motivo}\n"
            "Nunca se commitea, mergea ni hace push sobre main (ver 'Ramas' en CLAUDE.md). "
            "Crea antes una rama: git switch -c <feat|fix|chore|docs>/<slug-en-ingles>. "
            "La integracion en main la hace el usuario.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
