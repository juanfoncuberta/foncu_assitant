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
