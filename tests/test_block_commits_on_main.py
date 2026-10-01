"""
Tests del hook PreToolUse que impide commitear, mergear o hacer push sobre main.

Se usan repos git reales en tmp_path: lo que se protege es el comportamiento contra
git de verdad, y simularlo esconderia justo los fallos que importan.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "block_commits_on_main.py"

_spec = importlib.util.spec_from_file_location("block_commits_on_main", HOOK)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """Repo con un commit inicial, en la rama main."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "--allow-empty", "-m", "init")
    return tmp_path


@pytest.fixture
def repo_en_rama(repo):
    _git(repo, "switch", "-q", "-c", "feat/algo")
    return repo


# ---------------------------------------------------------------------------
# Lo que se bloquea estando en main
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "comando",
    [
        'git commit -m "add tests"',
        "git add -A && git commit -m x",
        'git -c user.name=x commit -m "y"',
        "GIT_AUTHOR_NAME=x git commit -m y",
        "git merge feat/algo",
        "git cherry-pick abc123",
        "git revert HEAD",
        "git am 0001.patch",
        "git rebase feat/algo",
        "git pull",
        "git push",
        "git push -u origin HEAD",
    ],
)
def test_escrituras_en_main_se_bloquean(repo, comando):
    assert hook.motivo_de_bloqueo(comando, str(repo)) is not None


def test_master_tambien_esta_protegida(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "master")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "--allow-empty", "-m", "init")
    assert "master" in hook.motivo_de_bloqueo("git commit -m x", str(tmp_path))


def test_git_menos_c_apunta_a_otro_repo_en_main(repo_en_rama, tmp_path):
    # El cwd esta en una rama de trabajo, pero -C apunta a un repo en main.
    otro = tmp_path / "otro"
    otro.mkdir()
    _git(otro, "init", "-q", "-b", "main")
    _git(otro, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "--allow-empty", "-m", "init")
    assert hook.motivo_de_bloqueo(f"git -C {otro} commit -m x", str(repo_en_rama)) is not None


# ---------------------------------------------------------------------------
# Lo que se permite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "comando",
    [
        "git status",
        "git log --oneline -5",
        "git diff HEAD",
        "git fetch origin",
        "git branch -vv",
        "pytest -q",
        "echo 'git commit' > nota.txt",
    ],
)
def test_lecturas_y_otros_comandos_en_main_pasan(repo, comando):
    assert hook.motivo_de_bloqueo(comando, str(repo)) is None


@pytest.mark.parametrize(
    "comando",
    ['git commit -m "add tests"', "git push -u origin feat/algo", "git rebase main"],
)
def test_escrituras_en_rama_de_trabajo_pasan(repo_en_rama, comando):
    assert hook.motivo_de_bloqueo(comando, str(repo_en_rama)) is None


@pytest.mark.parametrize(
    "comando",
    [
        "git switch -c chore/x && git add -A && git commit -m y",
        "git checkout -b fix/x; git commit -m y",
    ],
)
def test_crear_rama_antes_de_commitear_en_la_misma_linea_pasa(repo, comando):
    assert hook.motivo_de_bloqueo(comando, str(repo)) is None


def test_fuera_de_un_repo_git_no_bloquea(tmp_path):
    assert hook.motivo_de_bloqueo("git commit -m x", str(tmp_path)) is None


# ---------------------------------------------------------------------------
# Push explicito a main desde cualquier rama
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "comando",
    [
        "git push origin main",
        "git push origin HEAD:main",
        "git push --force origin feat/algo:main",
        "git push origin +feat/algo:refs/heads/main",
        "git switch -c x && git push origin x:main",
    ],
)
def test_push_explicito_a_main_se_bloquea_desde_cualquier_rama(repo_en_rama, comando):
    assert hook.motivo_de_bloqueo(comando, str(repo_en_rama)) is not None


def test_push_a_rama_que_contiene_main_en_el_nombre_pasa(repo_en_rama):
    assert hook.motivo_de_bloqueo("git push origin feat/main-tests", str(repo_en_rama)) is None


# ---------------------------------------------------------------------------
# Contrato con Claude Code: stdin JSON, exit 2 y motivo por stderr
# ---------------------------------------------------------------------------


def _ejecutar_hook(payload) -> subprocess.CompletedProcess:
    entrada = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(HOOK)], input=entrada, capture_output=True, text=True
    )


def test_hook_bloquea_con_exit_2_y_explica_por_stderr(repo):
    res = _ejecutar_hook(
        {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "cwd": str(repo)}
    )
    assert res.returncode == 2
    assert "BLOQUEADO" in res.stderr
    assert "git switch -c" in res.stderr


def test_hook_deja_pasar_con_exit_0(repo_en_rama):
    res = _ejecutar_hook(
        {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "cwd": str(repo_en_rama)}
    )
    assert res.returncode == 0


def test_hook_con_payload_ilegible_no_bloquea():
    assert _ejecutar_hook("no es json").returncode == 0


def test_hook_esta_registrado_para_bash_en_settings():
    settings = json.loads((HOOK.parent.parent / "settings.json").read_text(encoding="utf-8"))
    comandos_bash = [
        h["command"]
        for entrada in settings["hooks"]["PreToolUse"]
        if entrada["matcher"] == "Bash"
        for h in entrada["hooks"]
    ]
    assert "python3 .claude/hooks/block_commits_on_main.py" in comandos_bash
