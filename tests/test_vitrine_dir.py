"""Tests for where vitrine keeps its study store.

Real repositories and worktrees are created with git, so these also pin the
on-disk format that resolution reads instead of asking git.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from vitrine._utils import get_vitrine_dir

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repository(tmp_path, monkeypatch) -> Path:
    monkeypatch.delenv("VITRINE_DATA_DIR", raising=False)
    root = (tmp_path / "project").resolve()
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "commit", "--quiet", "--allow-empty", "-m", "init")
    return root


def _add_worktree(repository: Path, path: Path) -> Path:
    _git(repository, "worktree", "add", "--quiet", str(path), "-b", path.name)
    return path


class TestLinkedWorktree:
    def test_shares_the_main_checkouts_store(self, repository, tmp_path, monkeypatch):
        (repository / ".vitrine").mkdir()
        worktree = _add_worktree(repository, tmp_path / "elsewhere" / "feature")

        monkeypatch.chdir(worktree)

        assert get_vitrine_dir() == repository / ".vitrine"

    def test_creates_the_store_in_the_main_checkout(
        self, repository, tmp_path, monkeypatch
    ):
        worktree = _add_worktree(repository, tmp_path / "elsewhere" / "feature")
        (worktree / "analysis").mkdir()

        monkeypatch.chdir(worktree / "analysis")

        assert get_vitrine_dir() == repository / ".vitrine"

    def test_two_worktrees_resolve_to_one_store(
        self, repository, tmp_path, monkeypatch
    ):
        first = _add_worktree(repository, tmp_path / "worktrees" / "first")
        second = _add_worktree(repository, tmp_path / "worktrees" / "second")

        monkeypatch.chdir(first)
        from_first = get_vitrine_dir()
        monkeypatch.chdir(second)

        assert get_vitrine_dir() == from_first == repository / ".vitrine"

    def test_keeps_a_store_the_worktree_already_has(
        self, repository, tmp_path, monkeypatch
    ):
        (repository / ".vitrine").mkdir()
        worktree = _add_worktree(repository, tmp_path / "elsewhere" / "feature")
        (worktree / ".vitrine").mkdir()

        monkeypatch.chdir(worktree)

        assert get_vitrine_dir() == worktree / ".vitrine"

    def test_ignores_a_store_above_the_worktree(
        self, repository, tmp_path, monkeypatch
    ):
        holder = tmp_path / "worktrees"
        (holder / ".vitrine").mkdir(parents=True)
        worktree = _add_worktree(repository, holder / "feature")

        monkeypatch.chdir(worktree)

        assert get_vitrine_dir() == repository / ".vitrine"

    def test_explicit_data_dir_still_wins(self, repository, tmp_path, monkeypatch):
        worktree = _add_worktree(repository, tmp_path / "elsewhere" / "feature")
        monkeypatch.setenv("VITRINE_DATA_DIR", str(tmp_path / "explicit"))

        monkeypatch.chdir(worktree)

        assert get_vitrine_dir() == tmp_path / "explicit"

    def test_worktree_of_a_bare_repository_keeps_its_own_store(
        self, repository, tmp_path, monkeypatch
    ):
        bare = tmp_path / "project.git"
        _git(tmp_path, "clone", "--quiet", "--bare", str(repository), str(bare))
        worktree = (tmp_path / "from-bare").resolve()
        _git(bare, "worktree", "add", "--quiet", str(worktree), "-b", "from-bare")

        monkeypatch.chdir(worktree)

        assert get_vitrine_dir() == worktree / ".vitrine"


class TestOutsideLinkedWorktrees:
    def test_main_checkout_subdirectory_is_unchanged(self, repository, monkeypatch):
        (repository / "analysis").mkdir()

        monkeypatch.chdir(repository / "analysis")

        assert get_vitrine_dir() == repository / "analysis" / ".vitrine"

    def test_main_checkout_finds_the_store_above(self, repository, monkeypatch):
        (repository / ".vitrine").mkdir()
        (repository / "analysis").mkdir()

        monkeypatch.chdir(repository / "analysis")

        assert get_vitrine_dir() == repository / ".vitrine"

    def test_plain_directory_is_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VITRINE_DATA_DIR", raising=False)
        plain = (tmp_path / "plain").resolve()
        plain.mkdir()

        monkeypatch.chdir(plain)

        assert get_vitrine_dir() == plain / ".vitrine"


class TestStoreStaysOutOfGit:
    """The store holds card data and the server's token; git must not see it."""

    def test_new_store_is_ignored_by_git(self, repository):
        from vitrine.study_manager import StudyManager

        store = repository / ".vitrine"
        manager = StudyManager(store)
        manager.get_or_create_study("demo")
        (store / ".server.json").write_text("{}")

        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert status == ""

        # What a tool that snapshots the folder does.
        _git(repository, "add", "-A")
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert staged == ""

    def test_git_clean_keeps_the_cards(self, repository):
        from vitrine.study_manager import StudyManager

        store = repository / ".vitrine"
        StudyManager(store).get_or_create_study("demo")
        _git(repository, "clean", "-fd")
        assert (store / "studies").is_dir()

    def test_an_existing_gitignore_is_left_alone(self, tmp_path):
        from vitrine._utils import keep_out_of_git

        store = tmp_path / ".vitrine"
        store.mkdir()
        (store / ".gitignore").write_text("studies/\n")
        keep_out_of_git(store)
        assert (store / ".gitignore").read_text() == "studies/\n"


class TestBrowserOpening:
    @pytest.mark.parametrize("value", ["0", "false", "No", " off "])
    def test_environment_can_turn_it_off(self, monkeypatch, value):
        from vitrine._utils import browser_opening_enabled

        monkeypatch.setenv("VITRINE_OPEN_BROWSER", value)
        assert browser_opening_enabled() is False

    @pytest.mark.parametrize("value", [None, "", "1", "true"])
    def test_default_is_on(self, monkeypatch, value):
        from vitrine._utils import browser_opening_enabled

        if value is None:
            monkeypatch.delenv("VITRINE_OPEN_BROWSER", raising=False)
        else:
            monkeypatch.setenv("VITRINE_OPEN_BROWSER", value)
        assert browser_opening_enabled() is True
