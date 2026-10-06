import json
import re
import subprocess

import pytest

import claude_code_executor as ce

BRANCH_RE = re.compile(r"^[a-z]+/[a-z0-9-]+-\d{6}$")


def cp(returncode=0, stdout="", stderr=""):
    """Build a CompletedProcess for use as subprocess.run return value."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _json_output(result="done", cost=0.01, session="abc"):
    return json.dumps({"result": result, "cost_usd": cost, "session_id": session})


# ---------------------------------------------------------------------------
# _classify_task
# ---------------------------------------------------------------------------


class TestClassifyTask:
    def test_fix_english_keyword(self):
        assert ce._classify_task("fix the broken login") == "fix"

    def test_fix_spanish_arregla(self):
        assert ce._classify_task("arregla el error de autenticación") == "fix"

    def test_fix_spanish_corrige(self):
        assert ce._classify_task("corrige la falla en el parser") == "fix"

    def test_feat_english_add(self):
        assert ce._classify_task("add new user dashboard") == "feat"

    def test_feat_english_implement(self):
        assert ce._classify_task("implement retry logic") == "feat"

    def test_feat_spanish_añade(self):
        assert ce._classify_task("añade soporte para múltiples idiomas") == "feat"

    def test_feat_spanish_crea(self):
        assert ce._classify_task("crea un módulo de exportación") == "feat"

    def test_chore_fallback(self):
        assert ce._classify_task("refactor the existing module") == "chore"

    def test_chore_no_keywords(self):
        assert ce._classify_task("update documentation and tests") == "chore"

    def test_fix_takes_priority_over_feat(self):
        assert ce._classify_task("fix and add feature") == "fix"


# ---------------------------------------------------------------------------
# _make_branch_name
# ---------------------------------------------------------------------------


class TestMakeBranchName:
    def test_prefix_fix(self):
        assert ce._make_branch_name("fix broken login").startswith("fix/")

    def test_prefix_feat(self):
        assert ce._make_branch_name("add new feature").startswith("feat/")

    def test_prefix_chore(self):
        assert ce._make_branch_name("refactor module").startswith("chore/")

    def test_branch_format_matches_pattern(self):
        assert BRANCH_RE.match(ce._make_branch_name("add login handler"))

    def test_accents_removed(self):
        name = ce._make_branch_name("añadir función de autenticación")
        assert "ñ" not in name
        assert "ó" not in name
        assert "ú" not in name

    def test_enye_removed(self):
        name = ce._make_branch_name("implementar función")
        assert "ñ" not in name

    def test_slug_contains_meaningful_words(self):
        name = ce._make_branch_name("add login handler")
        slug = name.split("/", 1)[1]
        assert "login" in slug
        assert "handler" in slug

    def test_stop_words_excluded(self):
        name = ce._make_branch_name("add the new feature for users")
        slug = name.split("/", 1)[1]
        assert "-the-" not in slug
        assert "-for-" not in slug

    def test_no_special_chars_in_slug(self):
        name = ce._make_branch_name("fix: crash on startup!")
        assert re.match(r"^[a-z0-9/\-]+$", name)


# ---------------------------------------------------------------------------
# check_git_status
# ---------------------------------------------------------------------------


class TestCheckGitStatus:
    def test_no_git_directory(self, tmp_path):
        result = ce.check_git_status(str(tmp_path))
        assert result == {"is_git": False, "is_clean": False}

    def test_clean_repo(self, tmp_path, mocker):
        (tmp_path / ".git").mkdir()
        mocker.patch("subprocess.run", return_value=cp(0, ""))
        result = ce.check_git_status(str(tmp_path))
        assert result == {"is_git": True, "is_clean": True}

    def test_dirty_repo(self, tmp_path, mocker):
        (tmp_path / ".git").mkdir()
        mocker.patch("subprocess.run", return_value=cp(0, " M modified_file.py\n"))
        result = ce.check_git_status(str(tmp_path))
        assert result == {"is_git": True, "is_clean": False}

    def test_git_command_fails(self, tmp_path, mocker):
        (tmp_path / ".git").mkdir()
        mocker.patch("subprocess.run", return_value=cp(128, "", "fatal: not a git repo"))
        result = ce.check_git_status(str(tmp_path))
        assert result == {"is_git": True, "is_clean": False}


# ---------------------------------------------------------------------------
# get_git_diff
# ---------------------------------------------------------------------------


class TestGetGitDiff:
    def test_with_changes(self, mocker):
        mocker.patch("subprocess.run", return_value=cp(0, "diff --git a/f b/f\n+added line"))
        result = ce.get_git_diff("/some/path")
        assert "added line" in result

    def test_no_changes(self, mocker):
        mocker.patch("subprocess.run", return_value=cp(0, ""))
        result = ce.get_git_diff("/some/path")
        assert result == "(sin cambios en el árbol de trabajo)"

    def test_truncated_at_5000_chars(self, mocker):
        long_diff = "x" * 6000
        mocker.patch("subprocess.run", return_value=cp(0, long_diff))
        result = ce.get_git_diff("/some/path")
        assert result == "x" * 5000 + "…(truncado)"

    def test_exception_returns_fallback(self, mocker):
        mocker.patch("subprocess.run", side_effect=Exception("boom"))
        result = ce.get_git_diff("/some/path")
        assert result == "(no se pudo obtener el diff)"


# ---------------------------------------------------------------------------
# execute_task
# ---------------------------------------------------------------------------


class TestExecuteTask:
    def test_success(self, mocker):
        mocker.patch(
            "subprocess.run",
            side_effect=[
                cp(0, _json_output("task result")),  # claude -p
                cp(0, ""),                            # git diff HEAD (in get_git_diff)
            ],
        )
        result = ce.execute_task("do something", "/path")
        assert result["error"] is None
        assert result["result"] == "task result"
        assert result["cost_usd"] == 0.01
        assert result["session_id"] == "abc"

    def test_timeout(self, mocker):
        mocker.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=600),
        )
        result = ce.execute_task("do something", "/path")
        assert "Timeout" in result["error"]
        assert result["result"] is None

    def test_claude_not_in_path(self, mocker):
        mocker.patch("subprocess.run", side_effect=FileNotFoundError)
        result = ce.execute_task("do something", "/path")
        assert "claude" in result["error"].lower()
        assert result["result"] is None

    def test_nonzero_exit_code(self, mocker):
        mocker.patch("subprocess.run", return_value=cp(1, "", "internal error"))
        result = ce.execute_task("do something", "/path")
        assert "code 1" in result["error"]
        assert result["result"] is None

    def test_malformed_json_output(self, mocker):
        mocker.patch("subprocess.run", return_value=cp(0, "this is not json"))
        result = ce.execute_task("do something", "/path")
        assert "not valid JSON" in result["error"]
        assert result["result"] is None

    def test_allows_only_git_and_pytest_commands(self, mocker):
        mock_run = mocker.patch(
            "subprocess.run",
            side_effect=[cp(0, _json_output("ok")), cp(0, "")],
        )
        ce.execute_task("do something", "/path")
        args = mock_run.call_args_list[0].args[0]
        assert "--allowedTools" in args
        allowed = args[args.index("--allowedTools") + 1].split(",")
        assert "Bash(git commit:*)" in allowed
        assert "Bash(pytest:*)" in allowed
        # Nunca Bash libre ni comandos que salgan de la rama o destruyan trabajo.
        assert "Bash" not in allowed
        for peligroso in ("push", "checkout", "reset", "rm ", "merge"):
            assert not any(peligroso in a for a in allowed), peligroso


class TestBuildExecPrompt:
    def test_names_the_skill(self):
        assert "ejecutar-tarea-dev" in ce._build_exec_prompt("add x", "edit foo.py")

    def test_includes_task_and_plan(self):
        prompt = ce._build_exec_prompt("add x handler", "Would edit foo.py only.")
        assert "add x handler" in prompt
        assert "Would edit foo.py only." in prompt

    def test_keeps_commit_instructions(self):
        assert ce._COMMIT_INSTRUCTIONS in ce._build_exec_prompt("add x", "plan")


class TestFallbackCommitMessage:
    def test_keeps_prefix_and_is_english(self):
        msg = ce._fallback_commit_message("feat/anadir-docstring-funcion-fallback-135351")
        assert msg == "feat: apply changes from automated dev task"

    def test_does_not_leak_the_spanish_slug(self):
        msg = ce._fallback_commit_message("fix/arreglar-login-roto-120000")
        assert msg.startswith("fix: ")
        assert "arreglar" not in msg

    def test_branch_without_prefix_falls_back_to_chore(self):
        assert ce._fallback_commit_message("rama-rara").startswith("chore: ")


# ---------------------------------------------------------------------------
# _fallback_commit
# ---------------------------------------------------------------------------


class TestFallbackCommit:
    def test_noop_when_clean(self, mocker):
        mock_run = mocker.patch("subprocess.run", return_value=cp(0, ""))
        result = ce._fallback_commit("feat/my-task-120000", "/path")
        assert result is None
        all_cmds = [" ".join(c.args[0]) for c in mock_run.call_args_list]
        assert not any("add" in cmd for cmd in all_cmds)
        assert not any("commit" in cmd for cmd in all_cmds)

    def test_commits_when_dirty(self, mocker):
        mock_run = mocker.patch(
            "subprocess.run",
            side_effect=[
                cp(0, "M src/file.py"),  # git status → dirty
                cp(0, ""),               # git add -A
                cp(0, ""),               # git commit
            ],
        )
        result = ce._fallback_commit("feat/my-task-120000", "/path")
        assert result is None
        all_cmds = [" ".join(c.args[0]) for c in mock_run.call_args_list]
        assert any("add" in cmd for cmd in all_cmds)
        assert any("commit" in cmd for cmd in all_cmds)

    def test_returns_error_string_if_add_fails(self, mocker):
        mocker.patch(
            "subprocess.run",
            side_effect=[
                cp(0, "M src/file.py"),       # git status → dirty
                cp(128, "", "lock error"),    # git add fails
            ],
        )
        result = ce._fallback_commit("feat/task-120000", "/path")
        assert result is not None
        assert "add" in result.lower()


# ---------------------------------------------------------------------------
# _get_plan
# ---------------------------------------------------------------------------


class TestGetPlan:
    def test_success(self, mocker):
        mocker.patch(
            "subprocess.run",
            return_value=cp(0, _json_output("I would edit foo.py to add X.")),
        )
        plan = ce._get_plan("add feature", "/path")
        assert plan == "I would edit foo.py to add X."

    def test_uses_disallowed_tools_flag(self, mocker):
        mock_run = mocker.patch("subprocess.run", return_value=cp(0, _json_output("plan")))
        ce._get_plan("add feature", "/path")
        args = mock_run.call_args.args[0]
        assert "--disallowedTools" in args
        idx = args.index("--disallowedTools")
        assert args[idx + 1] == "Edit,Write,Bash"

    def test_nonzero_exit_returns_fallback(self, mocker):
        mocker.patch("subprocess.run", return_value=cp(1, "", "error"))
        plan = ce._get_plan("add feature", "/path")
        assert "no se pudo obtener el plan" in plan

    def test_timeout_returns_fallback(self, mocker):
        mocker.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=120),
        )
        plan = ce._get_plan("add feature", "/path")
        assert "no se pudo obtener el plan" in plan

    def test_malformed_json_returns_fallback(self, mocker):
        mocker.patch("subprocess.run", return_value=cp(0, "not json"))
        plan = ce._get_plan("add feature", "/path")
        assert "no se pudo obtener el plan" in plan

    def test_empty_result_returns_placeholder(self, mocker):
        mocker.patch("subprocess.run", return_value=cp(0, _json_output("")))
        plan = ce._get_plan("add feature", "/path")
        assert plan == "(plan vacío)"


# ---------------------------------------------------------------------------
# execute_task_on_branch
# ---------------------------------------------------------------------------


def _branch_exec_calls(
    original="main",
    branch_stdout="",
    claude_rc=0,
    claude_stdout=None,
    porcelain_after="",
):
    """Build a side_effect list for execute_task_on_branch (clean, no fallback commit)."""
    if claude_stdout is None:
        claude_stdout = _json_output()

    calls = [
        cp(0, original),        # git rev-parse --abbrev-ref HEAD
        cp(0, branch_stdout),   # git checkout -b <branch>
        cp(claude_rc, claude_stdout, "" if claude_rc == 0 else "error"),  # claude -p
    ]
    if claude_rc == 0:
        calls.append(cp(0, ""))  # git diff HEAD (inside get_git_diff)
    calls += [
        cp(0, "+some diff"),     # git diff <original> (branch diff)
        cp(0, porcelain_after),  # git status --porcelain (fallback check, only on success)
        cp(0, ""),               # git status --porcelain (inside _checkout_safe)
        cp(0, ""),               # git checkout <original> (inside _checkout_safe)
    ]
    if claude_rc != 0:
        # On failure result["error"] is not None, fallback check is skipped
        # so remove the "fallback check" entry we added
        del calls[-3]
    return calls


class TestExecuteTaskOnBranch:
    @pytest.fixture(autouse=True)
    def _plan_mock(self, mocker):
        mocker.patch("claude_code_executor._get_plan", return_value="mocked plan")
        mocker.patch(
            "claude_code_executor._revisar_con_reintento",
            return_value={"texto": "mocked review", "estado": "completa",
                          "segundos": 1.0, "lineas": 3},
        )

    def test_branch_prefix_matches_classify_task(self, mocker):
        mock_run = mocker.patch("subprocess.run", side_effect=_branch_exec_calls())
        ce.execute_task_on_branch("add new feature", "/path")
        checkout_b = next(
            " ".join(c.args[0])
            for c in mock_run.call_args_list
            if "checkout" in c.args[0] and "-b" in c.args[0]
        )
        branch_name = checkout_b.split("-b ")[-1] if "-b " in checkout_b else checkout_b.rsplit(" ", 1)[-1]
        # Get the actual -b argument from the args list
        for c in mock_run.call_args_list:
            args = c.args[0]
            if "checkout" in args and "-b" in args:
                idx = args.index("-b")
                branch_name = args[idx + 1]
                break
        assert branch_name.startswith("feat/")

    def test_branch_prefix_fix_for_fix_task(self, mocker):
        mock_run = mocker.patch("subprocess.run", side_effect=_branch_exec_calls())
        ce.execute_task_on_branch("fix the broken auth", "/path")
        for c in mock_run.call_args_list:
            args = c.args[0]
            if "checkout" in args and "-b" in args:
                idx = args.index("-b")
                assert args[idx + 1].startswith("fix/")
                break

    def test_returns_to_original_branch_on_success(self, mocker):
        mock_run = mocker.patch("subprocess.run", side_effect=_branch_exec_calls(original="develop"))
        ce.execute_task_on_branch("add feature", "/path")
        last_checkout = next(
            c.args[0]
            for c in reversed(mock_run.call_args_list)
            if "checkout" in c.args[0]
        )
        assert "develop" in last_checkout

    def test_returns_to_original_branch_on_failure(self, mocker):
        calls = _branch_exec_calls(original="main", claude_rc=1)
        mock_run = mocker.patch("subprocess.run", side_effect=calls)
        result = ce.execute_task_on_branch("add feature", "/path")
        assert result["error"] is not None
        last_checkout = next(
            c.args[0]
            for c in reversed(mock_run.call_args_list)
            if "checkout" in c.args[0]
        )
        assert "main" in last_checkout

    def test_no_merge_to_main(self, mocker):
        mock_run = mocker.patch("subprocess.run", side_effect=_branch_exec_calls())
        ce.execute_task_on_branch("add feature", "/path")
        all_cmds = [" ".join(c.args[0]) for c in mock_run.call_args_list]
        assert not any("merge" in cmd for cmd in all_cmds)

    def test_result_includes_branch_key(self, mocker):
        mocker.patch("subprocess.run", side_effect=_branch_exec_calls())
        result = ce.execute_task_on_branch("add feature", "/path")
        assert "branch" in result
        assert result["branch"].startswith("feat/")

    def test_fallback_commit_triggered_when_dirty_after_execution(self, mocker):
        """When claude leaves uncommitted changes, fallback commit is invoked."""
        calls = [
            cp(0, "main"),              # git rev-parse
            cp(0, ""),                  # git checkout -b
            cp(0, _json_output()),      # claude -p (success)
            cp(0, ""),                  # git diff HEAD
            cp(0, "+changes"),          # git diff main
            cp(0, "M file.py"),         # git status → dirty (triggers fallback)
            cp(0, "M file.py"),         # git status inside _fallback_commit
            cp(0, ""),                  # git add -A
            cp(0, ""),                  # git commit
            cp(0, ""),                  # git status inside _checkout_safe (now clean)
            cp(0, ""),                  # git checkout main
        ]
        mock_run = mocker.patch("subprocess.run", side_effect=calls)
        ce.execute_task_on_branch("add feature", "/path")
        all_cmds = [" ".join(c.args[0]) for c in mock_run.call_args_list]
        assert any("commit" in cmd for cmd in all_cmds)

    def test_fallback_commit_not_triggered_when_clean(self, mocker):
        """When the repo is clean after execution, no fallback commit happens."""
        mock_run = mocker.patch("subprocess.run", side_effect=_branch_exec_calls(porcelain_after=""))
        ce.execute_task_on_branch("add feature", "/path")
        # The only commit-like call should NOT be a git commit
        all_cmds = [" ".join(c.args[0]) for c in mock_run.call_args_list]
        assert not any(cmd.startswith("git commit") for cmd in all_cmds)

    def test_exec_prompt_carries_plan_and_skill(self, mocker):
        mock_run = mocker.patch("subprocess.run", side_effect=_branch_exec_calls())
        ce.execute_task_on_branch("add feature", "/path")
        claude_call = next(c.args[0] for c in mock_run.call_args_list if c.args[0][0] == "claude")
        prompt = claude_call[claude_call.index("-p") + 1]
        assert "mocked plan" in prompt
        assert "ejecutar-tarea-dev" in prompt

    def test_reviewer_runs_after_successful_execution(self, mocker):
        mocker.patch("subprocess.run", side_effect=_branch_exec_calls())
        result = ce.execute_task_on_branch("add feature", "/path")
        ce._revisar_con_reintento.assert_called_once()
        assert result["review"] == "mocked review"
        assert result["revision_estado"] == "completa"
        assert result["revision_valida"] is True


# ---------------------------------------------------------------------------
# _run_reviewer — el reviewer ve el diff completo y declara su cobertura (A3)
# ---------------------------------------------------------------------------


def _numstat(*registros):
    """Salida de `git diff --numstat -z`: 'anadidas\tborradas\truta\0' por archivo."""
    return "".join(f"{a}\t{b}\t{ruta}\0" for a, b, ruta in registros)


def _informe(rutas, revisados=None, total=None):
    total = len(rutas) if total is None else total
    revisados = total if revisados is None else revisados
    lineas = "\n".join(f"- {r}" for r in rutas)
    return (
        "SIN HALLAZGOS\n\n"
        f"COBERTURA: revisados {revisados} de {total} archivos (10 líneas de diff)\n"
        f"Archivos revisados:\n{lineas}"
    )


class FakeGit:
    """
    Sustituye a _git: devuelve el numstat dado y, para cada archivo, su diff. Guarda
    las llamadas para comprobar que se pide el diff entre ramas de cada archivo.
    """

    def __init__(self, diffs, numstat=None, rc_numstat=0, rc_diff=0, commits="0"):
        self.diffs = diffs  # {ruta: texto del diff}
        self.commits = commits  # salida de `git rev-list --count base..rama`
        self.numstat = numstat if numstat is not None else _numstat(
            *[(d.count("\n+"), d.count("\n-"), r) for r, d in diffs.items()]
        )
        self.rc_numstat = rc_numstat
        self.rc_diff = rc_diff
        self.calls = []

    def __call__(self, args, cwd, timeout=30, strip=True):
        self.calls.append(args)
        if "--numstat" in args:
            return self.rc_numstat, self.numstat, ""
        if args[0] == "rev-list":
            return 0, self.commits, ""
        ruta = args[-1]
        diff = self.diffs[ruta]
        return self.rc_diff, (diff.strip() if strip else diff), ""  # como el _git real


class TestRunReviewerCobertura:
    @pytest.fixture(autouse=True)
    def _entorno(self, mocker, monkeypatch, tmp_path):
        monkeypatch.delenv("REVIEWER_ENABLED", raising=False)
        mocker.patch("claude_code_executor.usage_log.record")
        # Carpeta temporal conocida para poder comprobar que se borra al final.
        self.tmp = tmp_path / "review"
        mocker.patch(
            "claude_code_executor.tempfile.mkdtemp",
            side_effect=lambda **kw: (self.tmp.mkdir(), str(self.tmp))[1],
        )

    def _lanzar(self, mocker, git, informe):
        mocker.patch.object(ce, "_git", side_effect=git)
        visto = {}

        def claude(cmd, **kwargs):
            # Copia lo que el reviewer tendria en disco mientras se ejecuta.
            visto["cmd"] = cmd
            visto["archivos"] = {
                p.name: p.read_text() for p in sorted(self.tmp.iterdir())
            }
            return cp(0, _json_output(informe))

        mocker.patch("subprocess.run", side_effect=claude)
        res = ce._run_reviewer("add feature", "plan", "feat/x-1", "main", "/repo")
        return res, visto

    def test_el_reviewer_recibe_el_diff_completo_aunque_supere_el_recorte(self, mocker):
        # Regresion A3: el diff que ve el usuario se recorta; el del reviewer nunca.
        grande = "diff --git a/a.py b/a.py\n" + "+x\n" * 4000
        assert len(grande) > ce.DISPLAY_DIFF_MAX_CHARS
        _, visto = self._lanzar(mocker, FakeGit({"a.py": grande}), _informe(["a.py"]))
        [contenido] = visto["archivos"].values()
        assert contenido == grande
        assert "truncado" not in contenido

    def test_el_diff_volcado_conserva_los_espacios_finales(self, mocker):
        diff = "diff --git a/a.py b/a.py\n+foo  "
        _, visto = self._lanzar(mocker, FakeGit({"a.py": diff}), _informe(["a.py"]))
        assert list(visto["archivos"].values()) == [diff]

    def test_cada_archivo_cambiado_va_en_el_encargo_con_su_diff(self, mocker):
        git = FakeGit({"a.py": "+a", "dir/b c.py": "+b"})
        _, visto = self._lanzar(mocker, git, _informe(["a.py", "dir/b c.py"]))
        prompt = visto["cmd"][visto["cmd"].index("-p") + 1]
        assert "2 files" in prompt
        for ruta in ("a.py", "dir/b c.py"):
            assert ruta in prompt
        for nombre in visto["archivos"]:
            assert str(self.tmp / nombre) in prompt
        # Diff entre ramas, archivo a archivo, con la ruta literal.
        por_archivo = [c for c in git.calls if "--numstat" not in c]
        # Tres puntos: solo lo que anade la rama desde que salio de la base.
        assert all("main...feat/x-1" in c and c[0] == "--literal-pathspecs" for c in por_archivo)
        assert any("main...feat/x-1" in c for c in git.calls if "--numstat" in c)
        assert [c[-1] for c in por_archivo] == ["a.py", "dir/b c.py"]

    def test_el_reviewer_puede_leer_la_carpeta_de_diffs_pero_no_escribir(self, mocker):
        _, visto = self._lanzar(mocker, FakeGit({"a.py": "+a"}), _informe(["a.py"]))
        cmd = visto["cmd"]
        dirs = [cmd[i + 1] for i, x in enumerate(cmd) if x == "--add-dir"]
        assert dirs == ["/repo", str(self.tmp)]
        assert cmd[cmd.index("--disallowedTools") + 1] == "Edit,Write"
        assert "--allowedTools" not in cmd  # no gana permisos de Bash

    def test_la_carpeta_temporal_se_borra_al_terminar(self, mocker):
        self._lanzar(mocker, FakeGit({"a.py": "+a"}), _informe(["a.py"]))
        assert not self.tmp.exists()

    def test_la_carpeta_temporal_se_borra_aunque_falle_la_cli(self, mocker):
        mocker.patch.object(ce, "_git", side_effect=FakeGit({"a.py": "+a"}))
        mocker.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 1))
        res = ce._run_reviewer("t", "p", "feat/x-1", "main", "/repo")
        assert "no terminó" in res
        assert not self.tmp.exists()

    def test_cobertura_completa_va_en_la_primera_linea(self, mocker):
        res, _ = self._lanzar(
            mocker, FakeGit({"a.py": "+a", "b.py": "+b"}), _informe(["a.py", "b.py"])
        )
        primera, resto = res.split("\n\n", 1)
        assert "revisados 2 de 2 archivos" in primera
        assert "INCOMPLETA" not in res
        assert resto.startswith("SIN HALLAZGOS")  # el informe va tal cual detras

    def test_reviewer_que_declara_menos_archivos_es_incompleto(self, mocker):
        res, _ = self._lanzar(
            mocker, FakeGit({"a.py": "+a", "b.py": "+b"}),
            _informe(["a.py", "b.py"], revisados=1),
        )
        assert res.startswith("⚠️ REVISIÓN INCOMPLETA")
        assert "1 de 2 archivos" in res.splitlines()[0]

    def test_reviewer_que_no_nombra_un_archivo_es_incompleto(self, mocker):
        # Dice "2 de 2" pero solo nombra uno: no se le cree.
        res, _ = self._lanzar(
            mocker, FakeGit({"a.py": "+a", "b.py": "+b"}),
            _informe(["a.py"], revisados=2, total=2),
        )
        primera = res.splitlines()[0]
        assert primera.startswith("⚠️ REVISIÓN INCOMPLETA")
        assert "b.py" in primera

    def test_una_ruta_contenida_en_otra_no_cuenta_como_revisada(self, mocker):
        # Nombrar tests/test_main.py no es nombrar main.py.
        res, _ = self._lanzar(
            mocker, FakeGit({"main.py": "+a", "tests/test_main.py": "+b"}),
            _informe(["tests/test_main.py"], revisados=2, total=2),
        )
        primera = res.splitlines()[0]
        assert primera.startswith("⚠️ REVISIÓN INCOMPLETA")
        assert "main.py" in primera.split("listar como revisados:")[1]

    def test_archivo_listado_como_sin_revisar_es_incompleto(self, mocker):
        informe = _informe(["a.py", "b.py"], revisados=2, total=2) + (
            "\nArchivos sin revisar:\n- b.py"
        )
        res, _ = self._lanzar(mocker, FakeGit({"a.py": "+a", "b.py": "+b"}), informe)
        assert res.startswith("⚠️ REVISIÓN INCOMPLETA")

    def test_rutas_mencionadas_en_hallazgos_no_suplen_la_lista(self, mocker):
        # b.py sale en el cuerpo del informe, pero no en "Archivos revisados".
        informe = "BLOQUEA EL COMMIT\n\nb.py linea 3: fallo\n\n" + _informe(
            ["a.py"], revisados=2, total=2
        ).split("\n\n", 1)[1]
        res, _ = self._lanzar(mocker, FakeGit({"a.py": "+a", "b.py": "+b"}), informe)
        assert res.startswith("⚠️ REVISIÓN INCOMPLETA")

    def test_acepta_rutas_entre_comillas_invertidas_y_en_negrita(self, mocker):
        informe = (
            "SIN HALLAZGOS\n\nCOBERTURA: revisados 1 de 1 archivos (3 líneas de diff)\n"
            "**Archivos revisados:**\n- `a.py`\n"
        )
        res, _ = self._lanzar(mocker, FakeGit({"a.py": "+a"}), informe)
        assert "INCOMPLETA" not in res

    @pytest.mark.parametrize("cabecera,vineta", [
        ("**Archivos revisados**:", "- "),
        ("Archivos revisados (2):", "* "),
        ("### Archivos revisados", "+ "),
    ])
    def test_tolera_variaciones_de_formato_markdown(self, mocker, cabecera, vineta):
        # Que un cambio de formato no dispare una falsa "REVISIÓN INCOMPLETA".
        informe = (
            "SIN HALLAZGOS\n\nCOBERTURA: revisados 2 de 2 archivos (3 líneas de diff)\n"
            f"{cabecera}\n{vineta}a.py\n{vineta}b.py\n"
        )
        res, _ = self._lanzar(mocker, FakeGit({"a.py": "+a", "b.py": "+b"}), informe)
        assert "INCOMPLETA" not in res

    def test_reviewer_que_cuenta_mal_el_total_es_incompleto(self, mocker):
        # Revisa los 7 que cree que hay, pero en realidad cambiaron 8.
        rutas = [f"f{i}.py" for i in range(8)]
        res, _ = self._lanzar(
            mocker, FakeGit({r: "+x" for r in rutas}),
            _informe(rutas, revisados=7, total=7),
        )
        assert res.startswith("⚠️ REVISIÓN INCOMPLETA")

    def test_reviewer_que_cree_que_hay_mas_archivos_es_incompleto(self, mocker):
        # "2 de 3" con 2 cambios: el reviewer trabaja con otra lista que la de git.
        res, _ = self._lanzar(
            mocker, FakeGit({"a.py": "+a", "b.py": "+b"}),
            _informe(["a.py", "b.py"], revisados=2, total=3),
        )
        assert res.startswith("⚠️ REVISIÓN INCOMPLETA")

    def test_informe_sin_linea_de_cobertura_es_incompleto(self, mocker):
        res, _ = self._lanzar(mocker, FakeGit({"a.py": "+a"}), "SIN HALLAZGOS\n\na.py ok")
        assert res.startswith("⚠️ REVISIÓN INCOMPLETA")
        assert "no se puede leer" in res.splitlines()[0]

    def test_sin_cambios_no_exige_cobertura(self, mocker):
        res, _ = self._lanzar(mocker, FakeGit({}, numstat=""), "SIN HALLAZGOS")
        assert "INCOMPLETA" not in res
        assert "revisados 0 de 0 archivos" in res.splitlines()[0]

    def test_diff_vacio_con_commits_propios_no_cuenta_como_revisado(self, mocker):
        # "0 de 0 archivos" no vale si la rama tiene commits: algo falla en la base.
        res, visto = self._lanzar(mocker, FakeGit({}, numstat="", commits="2"), "SIN HALLAZGOS")
        assert res.startswith("(sin revisar")
        assert visto == {}  # no se llego a lanzar el reviewer

    def test_diff_vacio_sin_poder_contar_commits_no_cuenta_como_revisado(self, mocker):
        res, _ = self._lanzar(mocker, FakeGit({}, numstat="", commits="fatal"), "SIN HALLAZGOS")
        assert res.startswith("(sin revisar")

    def test_revision_desactivada_avisa_como_no_revisada(self, monkeypatch):
        monkeypatch.setenv("REVIEWER_ENABLED", "false")
        res = ce._revision("t", "p", "feat/x-1", "main", "/repo")
        assert res["estado"] == "desactivada"
        assert res["texto"].startswith("❌ NO REVISADA")

    def test_archivo_binario_entra_en_la_lista(self, mocker):
        git = FakeGit({"logo.png": "Binary files differ"}, numstat=_numstat(("-", "-", "logo.png")))
        res, visto = self._lanzar(mocker, git, _informe(["logo.png"]))
        prompt = visto["cmd"][visto["cmd"].index("-p") + 1]
        assert "logo.png (binary" in prompt
        assert "INCOMPLETA" not in res

    def test_diff_grande_pide_revisar_archivo_a_archivo(self, mocker):
        lineas = ce.REVIEW_PER_FILE_THRESHOLD_LINES + 1
        git = FakeGit({"a.py": "+x"}, numstat=_numstat((lineas, 0, "a.py")))
        _, visto = self._lanzar(mocker, git, _informe(["a.py"]))
        prompt = visto["cmd"][visto["cmd"].index("-p") + 1]
        assert "file by file" in prompt

    def test_diff_pequeno_no_pide_revisar_archivo_a_archivo(self, mocker):
        git = FakeGit({"a.py": "+x"}, numstat=_numstat((3, 1, "a.py")))
        _, visto = self._lanzar(mocker, git, _informe(["a.py"]))
        prompt = visto["cmd"][visto["cmd"].index("-p") + 1]
        assert "file by file" not in prompt

    def test_no_poder_escribir_el_diff_no_lanza_el_reviewer_ni_revienta(self, mocker):
        mocker.patch.object(ce, "_git", side_effect=FakeGit({"a.py": "+a"}))
        mocker.patch("builtins.open", side_effect=OSError("disco lleno"))
        run = mocker.patch("subprocess.run")
        res = ce._run_reviewer("t", "p", "feat/x-1", "main", "/repo")
        run.assert_not_called()
        assert res.startswith("(sin revisar")
        assert not self.tmp.exists()

    @pytest.mark.parametrize("git", [
        FakeGit({"a.py": "+a"}, rc_numstat=128),
        FakeGit({"a.py": "+a"}, numstat="esto no es numstat"),
        FakeGit({"a.py": "+a"}, rc_diff=1),
    ], ids=["numstat-falla", "numstat-ilegible", "diff-de-un-archivo-falla"])
    def test_sin_lista_fiable_no_se_lanza_el_reviewer(self, mocker, git):
        # Mejor "sin revisar" que una revision sobre una lista incompleta.
        mocker.patch.object(ce, "_git", side_effect=git)
        run = mocker.patch("subprocess.run")
        res = ce._run_reviewer("t", "p", "feat/x-1", "main", "/repo")
        run.assert_not_called()
        assert res.startswith("(sin revisar")
        assert not self.tmp.exists()

    def test_fallo_de_la_cli_no_lleva_cabecera_de_cobertura(self, mocker):
        mocker.patch.object(ce, "_git", side_effect=FakeGit({"a.py": "+a"}))
        mocker.patch("subprocess.run", return_value=cp(1, "", "boom"))
        res = ce._run_reviewer("t", "p", "feat/x-1", "main", "/repo")
        assert res == "(la revisión falló — revisa el diff a mano)"


_REV_OK = {"texto": "Cobertura...\n\nSIN HALLAZGOS", "estado": "completa",
           "segundos": 12.0, "lineas": 40}


def _rev(estado, texto=None, segundos=10.0, lineas=40):
    return {"texto": texto or f"({estado})", "estado": estado,
            "segundos": segundos, "lineas": lineas}


class TestTimeoutRevision:
    def test_cambio_pequeno_usa_la_base(self):
        assert ce._timeout_revision(0) == ce.REVIEW_TIMEOUT_SECONDS

    def test_crece_con_las_lineas(self):
        assert ce._timeout_revision(100) > ce._timeout_revision(10) > ce.REVIEW_TIMEOUT_SECONDS

    def test_nunca_pasa_del_maximo(self):
        assert ce._timeout_revision(10**6) == ce.REVIEW_TIMEOUT_MAX_SECONDS

    def test_la_revision_usa_el_timeout_segun_el_tamano(self, mocker, monkeypatch):
        monkeypatch.delenv("REVIEWER_ENABLED", raising=False)
        mocker.patch("claude_code_executor.usage_log.record")
        mocker.patch.object(ce, "_git", side_effect=FakeGit(
            {"a.py": "+x"}, numstat=_numstat((300, 100, "a.py"))))
        run = mocker.patch("subprocess.run", return_value=cp(0, _json_output(_informe(["a.py"]))))
        res = ce._revision("t", "p", "feat/x-1", "main", "/repo")
        assert run.call_args.kwargs["timeout"] == ce._timeout_revision(400)
        assert res["estado"] == "completa"
        assert res["lineas"] == 400
        assert res["segundos"] is not None


class TestRevisarConReintento:
    def _con(self, mocker, *resultados):
        return mocker.patch.object(ce, "_revision", side_effect=list(resultados))

    def test_completa_a_la_primera_no_reintenta(self, mocker):
        rev = self._con(mocker, _REV_OK)
        res = ce._revisar_con_reintento("t", "p", "feat/x-1", "main", "/repo")
        assert res["estado"] == "completa"
        assert rev.call_count == 1

    def test_desactivada_no_reintenta(self, mocker):
        rev = self._con(mocker, _rev("desactivada"))
        res = ce._revisar_con_reintento("t", "p", "feat/x-1", "main", "/repo")
        assert res["estado"] == "desactivada"
        assert rev.call_count == 1

    @pytest.mark.parametrize("primera", ["incompleta", "sin_revisar"])
    def test_reintenta_archivo_a_archivo_con_el_tiempo_maximo(self, mocker, primera):
        rev = self._con(mocker, _rev(primera, segundos=240.0), dict(_REV_OK))
        res = ce._revisar_con_reintento("t", "p", "feat/x-1", "main", "/repo")
        assert rev.call_count == 2
        segunda = rev.call_args_list[1].kwargs
        assert segunda == {"por_archivo": True, "timeout": ce.REVIEW_TIMEOUT_MAX_SECONDS}
        assert res["estado"] == "completa"
        assert "segundo intento" in res["texto"]
        assert res["segundos"] == 252.0  # suma de los dos intentos

    def test_conserva_el_informe_parcial_del_primer_intento(self, mocker):
        self._con(mocker,
                  _rev("incompleta", "⚠️ REVISIÓN INCOMPLETA: 6 de 7\n\nBloqueante: hallazgo X"),
                  _rev("sin_revisar", "(la revisión no terminó en 600s — x)"))
        res = ce._revisar_con_reintento("t", "p", "feat/x-1", "main", "/repo")
        assert res["estado"] == "no_revisada"
        assert "Informe parcial del primer intento" in res["texto"]
        assert "Bloqueante: hallazgo X" in res["texto"]

    def test_dos_fallos_dejan_la_rama_no_revisada(self, mocker):
        self._con(mocker, _rev("sin_revisar", "(la revisión falló — x)"),
                  _rev("incompleta", "⚠️ REVISIÓN INCOMPLETA: 1 de 2\n\ninforme"))
        res = ce._revisar_con_reintento("t", "p", "feat/x-1", "main", "/repo")
        assert res["estado"] == "no_revisada"
        primera_linea = res["texto"].splitlines()[0]
        assert primera_linea.startswith("❌ NO REVISADA")
        assert "revisar_rama_dev" in primera_linea
        assert "(la revisión falló — x)" in res["texto"]
        assert "⚠️ REVISIÓN INCOMPLETA: 1 de 2" in res["texto"]
        assert "Informe parcial del segundo intento:" in res["texto"]


class TestRevisionObligatoriaEnElFlujo:
    @pytest.fixture(autouse=True)
    def _plan(self, mocker):
        mocker.patch("claude_code_executor._get_plan", return_value="plan")

    def test_rama_no_revisada_no_es_valida_y_queda_en_dev_log(self, mocker):
        mocker.patch.object(ce, "_revisar_con_reintento",
                            return_value=_rev("no_revisada", "❌ NO REVISADA: x"))
        mocker.patch("subprocess.run", side_effect=_branch_exec_calls(original="develop"))
        res = ce.execute_task_on_branch("add feature", "/path")
        assert res["revision_valida"] is False
        assert res["revision_estado"] == "no_revisada"
        assert res["review"].startswith("❌ NO REVISADA")
        [pendiente] = ce.get_unreviewed_dev_log_entries()
        assert (pendiente["branch"], pendiente["base_branch"], pendiente["directory"]) == (
            res["branch"], "develop", "/path")

    def test_revision_completa_guarda_duracion_y_lineas(self, mocker):
        mocker.patch.object(ce, "_revisar_con_reintento", return_value=_REV_OK)
        mocker.patch("subprocess.run", side_effect=_branch_exec_calls())
        res = ce.execute_task_on_branch("add feature", "/path")
        assert res["revision_valida"] is True
        assert ce.get_unreviewed_dev_log_entries() == []
        import sqlite3 as _sq
        with _sq.connect(ce.DB_PATH) as conn:
            fila = conn.execute(
                "SELECT review_status, review_seconds, review_lines FROM dev_log").fetchone()
        assert fila == ("completa", 12.0, 40)

    def test_detached_head_no_crea_rama_ni_ejecuta(self, mocker):
        run = mocker.patch("subprocess.run", return_value=cp(0, "HEAD"))
        res = ce.execute_task_on_branch("add feature", "/path")
        assert "detached HEAD" in res["error"]
        assert run.call_count == 1  # solo el rev-parse
        assert res["branch"] is None

    def test_fallo_del_commit_automatico_deja_la_rama_no_revisada(self, mocker):
        revisar = mocker.patch.object(ce, "_revisar_con_reintento")
        calls = [
            cp(0, "main"), cp(0, ""), cp(0, _json_output()), cp(0, ""), cp(0, "+changes"),
            cp(0, "M file.py"),          # git status -> sucio, entra el fallback
            cp(0, "M file.py"),          # git status dentro de _fallback_commit
            cp(0, ""),                   # git add -A
            cp(1, "", "boom"),           # git commit falla
            cp(0, ""), cp(0, ""),        # _checkout_safe
        ]
        mocker.patch("subprocess.run", side_effect=calls)
        res = ce.execute_task_on_branch("add feature", "/path")
        revisar.assert_not_called()
        assert res["revision_valida"] is False
        assert res["revision_estado"] == "no_revisada"
        assert res["review"].startswith("❌ NO REVISADA")


class TestDevLogRevision:
    def _guardar(self, branch, estado, directory="/repo", base="main"):
        ce._save_dev_log("tarea", branch, "resumen", plan="plan", base_branch=base,
                         directory=directory, revision=_rev(estado))

    def test_lista_solo_las_no_completas_con_carpeta(self):
        self._guardar("feat/a-1", "completa")
        self._guardar("feat/b-2", "no_revisada")
        self._guardar("feat/c-3", "desactivada")
        ce._save_dev_log("antigua", "feat/vieja-4", "resumen")  # sin carpeta ni base
        assert [e["branch"] for e in ce.get_unreviewed_dev_log_entries()] == [
            "feat/c-3", "feat/b-2"]

    def test_una_revision_nueva_la_saca_de_la_lista(self):
        self._guardar("feat/b-2", "no_revisada")
        ce._actualizar_revision_dev_log("feat/b-2", _REV_OK)
        assert ce.get_unreviewed_dev_log_entries() == []
        assert ce.get_dev_log_entry("feat/b-2")["review_status"] == "completa"

    def test_get_dev_log_entry_devuelve_lo_necesario_para_revisar(self):
        self._guardar("feat/b-2", "no_revisada", directory="/srv/app", base="develop")
        e = ce.get_dev_log_entry("feat/b-2")
        assert (e["task_content"], e["plan"], e["base_branch"], e["directory"]) == (
            "tarea", "plan", "develop", "/srv/app")
        assert ce.get_dev_log_entry("no/existe-0") is None

    def test_migra_una_tabla_antigua(self):
        import sqlite3 as _sq
        with _sq.connect(ce.DB_PATH) as conn:
            conn.execute("CREATE TABLE dev_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "task_content TEXT NOT NULL, branch TEXT NOT NULL, summary TEXT NOT NULL, "
                         "created_at TEXT NOT NULL DEFAULT (datetime('now')))")
            conn.execute("INSERT INTO dev_log (task_content, branch, summary) "
                         "VALUES ('vieja', 'feat/v-1', 's')")
        assert ce.get_dev_log_entry("feat/v-1")["review_status"] == ""
        assert ce.get_unreviewed_dev_log_entries() == []


@pytest.fixture()
def repo_git(tmp_path):
    """Repo git real: main con un commit y una rama feat/x-1 con otro encima."""
    import shutil as _sh
    if not _sh.which("git"):
        pytest.skip("git no disponible")
    repo = tmp_path / "repo"
    repo.mkdir()

    def g(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (repo / "a.py").write_text("a\n"); g("add", "."); g("commit", "-qm", "base")
    g("checkout", "-qb", "feat/x-1")
    (repo / "b.py").write_text("b\n"); g("add", "."); g("commit", "-qm", "rama")
    g("checkout", "-q", "main")
    return repo, g


class TestEstadoRama:
    def test_rama_pendiente(self, repo_git):
        repo, _ = repo_git
        assert ce.estado_rama(str(repo), "feat/x-1", "main") == "pendiente"

    def test_rama_integrada(self, repo_git):
        repo, g = repo_git
        g("merge", "-q", "--ff-only", "feat/x-1")
        assert ce.estado_rama(str(repo), "feat/x-1", "main") == "integrada"

    def test_rama_borrada(self, repo_git):
        repo, _ = repo_git
        assert ce.estado_rama(str(repo), "feat/no-1", "main") == "no_existe"

    def test_base_head_no_da_revision_vacia_por_buena(self, mocker, repo_git, monkeypatch):
        # Detached HEAD: estando en la rama, HEAD...rama sale vacio (y HEAD..rama no
        # tiene commits), asi que se rechaza "HEAD" como base de forma explicita.
        repo, g = repo_git
        g("checkout", "-q", "feat/x-1")
        monkeypatch.delenv("REVIEWER_ENABLED", raising=False)
        claude = mocker.patch.object(ce, "_lanzar_reviewer")
        res = ce._revision("t", "p", "feat/x-1", "HEAD", str(repo))
        assert res["estado"] == "sin_revisar"
        assert "detached HEAD" in res["texto"]
        claude.assert_not_called()

    def test_el_manifiesto_ignora_lo_que_avanzo_la_base(self, repo_git):
        # Revisar dias despues: main ya tiene commits nuevos que no son de la rama.
        repo, g = repo_git
        (repo / "c.py").write_text("c\n"); g("add", "."); g("commit", "-qm", "main avanza")
        manifiesto = ce._manifiesto_diff("main", "feat/x-1", str(repo))
        assert [a["path"] for a in manifiesto] == ["b.py"]


class TestRevisarRama:
    def _entrada(self, branch="feat/x-1"):
        return {"task_content": "t", "branch": branch, "plan": "p", "base_branch": "main",
                "directory": "/x", "review_status": "no_revisada", "created_at": ""}

    def test_rama_borrada_da_error_sin_revisar(self, mocker, repo_git):
        repo, _ = repo_git
        revisar = mocker.patch.object(ce, "_revisar_con_reintento")
        res = ce.revisar_rama(self._entrada("feat/no-1"), str(repo))
        assert "ya no existe" in res["error"]
        revisar.assert_not_called()

    def test_revisa_sin_cambiar_de_rama_y_actualiza_dev_log(self, mocker, repo_git):
        repo, _ = repo_git
        ce._save_dev_log("t", "feat/x-1", "s", plan="p", base_branch="main",
                         directory=str(repo), revision=_rev("no_revisada"))
        revisar = mocker.patch.object(ce, "_revisar_con_reintento", return_value=_REV_OK)
        res = ce.revisar_rama(self._entrada(), str(repo))
        revisar.assert_called_once_with("t", "p", "feat/x-1", "main", str(repo))
        assert res["revision_valida"] is True
        assert "aviso" not in res
        assert ce.get_dev_log_entry("feat/x-1")["review_status"] == "completa"
        actual = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo,
                                capture_output=True, text=True).stdout.strip()
        assert actual == "main"

    @pytest.mark.parametrize("merge", [["--ff-only"], ["--no-ff", "-m", "merge"]])
    def test_rama_ya_integrada_no_se_revisa_ni_se_marca_revisada(self, mocker, repo_git, merge):
        repo, g = repo_git
        ce._save_dev_log("t", "feat/x-1", "s", plan="p", base_branch="main",
                         directory=str(repo), revision=_rev("no_revisada"))
        g("merge", "-q", *merge, "feat/x-1")
        revisar = mocker.patch.object(ce, "_revisar_con_reintento", return_value=_REV_OK)
        res = ce.revisar_rama(self._entrada(), str(repo))
        assert "ya está integrada" in res["error"]
        assert res["revision_valida"] is False
        revisar.assert_not_called()
        assert ce.get_dev_log_entry("feat/x-1")["review_status"] == "no_revisada"


class TestDiffParaMostrar:
    def test_diff_de_rama_se_recorta_solo_para_mostrar(self, mocker):
        mocker.patch("claude_code_executor._get_plan", return_value="plan")
        mocker.patch("claude_code_executor._revisar_con_reintento", return_value=_REV_OK)
        largo = "+" * (ce.DISPLAY_DIFF_MAX_CHARS + 10)
        calls = _branch_exec_calls()
        calls[4] = cp(0, largo)  # git diff <original>
        mocker.patch("subprocess.run", side_effect=calls)
        res = ce.execute_task_on_branch("add feature", "/path")
        assert res["git_diff"] == "+" * ce.DISPLAY_DIFF_MAX_CHARS + "…(truncado)"


# ---------------------------------------------------------------------------
# dev_log — _save_dev_log / get_recent_dev_log_entries
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_dev_db(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, "DB_PATH", str(tmp_path / "test.db"))


class TestDevLog:
    def test_save_and_retrieve(self, isolated_dev_db):
        ce._save_dev_log("add feature X", "feat/feature-x-120000", "Added feature X to solve Y.")
        entries = ce.get_recent_dev_log_entries(since_days=1)
        assert len(entries) == 1
        e = entries[0]
        assert e["task_content"] == "add feature X"
        assert e["branch"] == "feat/feature-x-120000"
        assert e["summary"] == "Added feature X to solve Y."
        assert e["plan"] == ""
        assert "created_at" in e

    def test_save_and_retrieve_with_plan(self, isolated_dev_db):
        ce._save_dev_log(
            "add feature X", "feat/feature-x-120000", "Added feature X to solve Y.",
            plan="Would edit foo.py to add the X handler.",
        )
        entries = ce.get_recent_dev_log_entries(since_days=1)
        assert entries[0]["plan"] == "Would edit foo.py to add the X handler."

    def test_empty_table_returns_empty_list(self, isolated_dev_db):
        assert ce.get_recent_dev_log_entries() == []

    def test_multiple_entries_ordered_newest_first(self, isolated_dev_db):
        ce._save_dev_log("task A", "feat/a-000001", "Summary A")
        ce._save_dev_log("task B", "feat/b-000002", "Summary B")
        entries = ce.get_recent_dev_log_entries(since_days=1)
        assert len(entries) == 2
        # Most recent insert (B) should come first (ORDER BY created_at DESC)
        assert entries[0]["task_content"] == "task B"
        assert entries[1]["task_content"] == "task A"

    def test_since_days_filters_old_entries(self, isolated_dev_db):
        import sqlite3 as _sq
        # Insert a recent and an old entry directly, bypassing datetime('now')
        conn = _sq.connect(ce.DB_PATH)
        conn.execute("CREATE TABLE IF NOT EXISTS dev_log (id INTEGER PRIMARY KEY AUTOINCREMENT, task_content TEXT NOT NULL, branch TEXT NOT NULL, summary TEXT NOT NULL, plan TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now')))")
        conn.execute("INSERT INTO dev_log (task_content, branch, summary, plan, created_at) VALUES (?, ?, ?, ?, ?)",
                     ("old task", "chore/old-000000", "old summary", "", "2000-01-01T00:00:00"))
        conn.execute("INSERT INTO dev_log (task_content, branch, summary, plan, created_at) VALUES (?, ?, ?, ?, ?)",
                     ("recent task", "feat/recent-000000", "recent summary", "", "2099-01-01T00:00:00"))
        conn.commit()
        conn.close()
        entries = ce.get_recent_dev_log_entries(since_days=7)
        contents = [e["task_content"] for e in entries]
        assert "recent task" in contents
        assert "old task" not in contents

    def test_all_expected_fields_present(self, isolated_dev_db):
        ce._save_dev_log("task", "fix/task-120000", "summary")
        entry = ce.get_recent_dev_log_entries(since_days=1)[0]
        for key in ("id", "task_content", "branch", "summary", "plan", "created_at"):
            assert key in entry

    def test_migration_adds_plan_column_to_existing_table(self, isolated_dev_db):
        """A pre-existing dev_log table (created before the 'plan' column existed) gets migrated."""
        import sqlite3 as _sq
        conn = _sq.connect(ce.DB_PATH)
        conn.execute("CREATE TABLE dev_log (id INTEGER PRIMARY KEY AUTOINCREMENT, task_content TEXT NOT NULL, branch TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')))")
        conn.commit()
        conn.close()

        ce._save_dev_log("legacy task", "chore/legacy-000000", "legacy summary")
        entries = ce.get_recent_dev_log_entries(since_days=1)
        assert entries[0]["plan"] == ""


class TestExecuteTaskOnBranchDevLog:
    @pytest.fixture(autouse=True)
    def _plan_mock(self, mocker):
        mocker.patch("claude_code_executor._get_plan", return_value="mocked plan")
        mocker.patch(
            "claude_code_executor._revisar_con_reintento",
            return_value={"texto": "mocked review", "estado": "completa",
                          "segundos": 1.0, "lineas": 3},
        )

    def test_saves_dev_log_on_success(self, mocker, tmp_path, monkeypatch):
        monkeypatch.setattr(ce, "DB_PATH", str(tmp_path / "test.db"))
        mocker.patch("subprocess.run", side_effect=_branch_exec_calls(
            claude_stdout=_json_output("The task was done by adding X because Y.")
        ))
        ce.execute_task_on_branch("add feature", "/path")
        entries = ce.get_recent_dev_log_entries(since_days=1)
        assert len(entries) == 1
        assert entries[0]["summary"] == "The task was done by adding X because Y."
        assert entries[0]["task_content"] == "add feature"
        assert entries[0]["plan"] == "mocked plan"

    def test_does_not_save_dev_log_on_error(self, mocker, tmp_path, monkeypatch):
        monkeypatch.setattr(ce, "DB_PATH", str(tmp_path / "test.db"))
        mocker.patch("subprocess.run", side_effect=_branch_exec_calls(claude_rc=1))
        ce.execute_task_on_branch("add feature", "/path")
        assert ce.get_recent_dev_log_entries(since_days=1) == []


class TestExtraerCoste:
    """
    Regresion real (28/09/2026): el codigo leia `cost_usd` y la CLI emite
    `total_cost_usd`, asi que TODAS las tareas de dev reportaron coste None durante
    meses. No se detecto porque los tests de arriba fabrican la respuesta con la clave
    que el codigo esperaba: validaban el mock, no la CLI.

    Estos tests usan el output literal de `claude -p --output-format json`.
    """

    def test_lee_total_cost_usd_del_formato_actual(self):
        data = {
            "type": "result",
            "subtype": "success",
            "result": "¡Hola!",
            "session_id": "abc",
            "total_cost_usd": 0.020686,
            "usage": {"input_tokens": 2340, "output_tokens": 30},
        }
        assert ce._extraer_coste(data) == 0.020686

    def test_acepta_cost_usd_de_versiones_antiguas(self):
        assert ce._extraer_coste({"result": "x", "cost_usd": 0.01}) == 0.01

    def test_prefiere_total_cost_usd_si_estan_los_dos(self):
        data = {"total_cost_usd": 0.02, "cost_usd": 0.01}
        assert ce._extraer_coste(data) == 0.02

    def test_devuelve_none_y_avisa_si_no_hay_ningun_campo_de_coste(self, caplog):
        assert ce._extraer_coste({"result": "x", "session_id": "y"}) is None
        assert "no devolvio ningun campo de coste" in caplog.text

    def test_coste_cero_no_se_confunde_con_ausente(self):
        # 0.0 es falsy: un `if not valor` lo trataria como si no estuviera.
        assert ce._extraer_coste({"total_cost_usd": 0.0}) == 0.0


class TestExtraerUso:

    def test_aplana_el_usage_del_formato_actual(self):
        data = {
            "usage": {
                "input_tokens": 2340,
                "output_tokens": 30,
                "cache_read_input_tokens": 16472,
                "cache_creation_input_tokens": 0,
            },
            "modelUsage": {"claude-opus-4-8[1m]": {"costUSD": 0.020686}},
        }
        uso = ce._extraer_uso(data)
        assert uso["input_tokens"] == 2340
        assert uso["output_tokens"] == 30
        assert uso["cache_read_input_tokens"] == 16472
        assert uso["model"] == "claude-opus-4-8[1m]"

    def test_devuelve_none_si_la_cli_no_informa_de_uso(self):
        assert ce._extraer_uso({"result": "x"}) is None

    def test_no_revienta_si_falta_modelUsage(self):
        uso = ce._extraer_uso({"usage": {"input_tokens": 10, "output_tokens": 2}})
        assert uso["model"] is None
        assert uso["input_tokens"] == 10
