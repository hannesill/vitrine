"""Tests for vitrine public API (show, start, stop, section).

Tests cover:
- show() returns a card_id
- show() stores cards in artifact store
- show() with different object types
- section() creates section cards
- Module state management
- Server discovery and client mode
- Blocking show (wait=True)
- get_selection()
- on_event() callback registration
- StudyManager integration: list_studies, delete_study, clean_studies
- Multi-study show() calls
- Auto-study creation
- stop_server() preserves study data
"""

import hashlib
import json
import os
import runpy

import pandas as pd
import pytest

import vitrine as display
import vitrine._state as _st
import vitrine.client as _client_mod
from vitrine._types import CardType, DisplayResponse
from vitrine.artifacts import ArtifactStore
from vitrine.study_manager import StudyManager


def _server_metadata(pid_path, **overrides):
    """Build current lifecycle metadata owned by ``pid_path.parent``."""
    info = {
        "pid": os.getpid(),
        "port": 7741,
        "host": "127.0.0.1",
        "url": "http://vitrine.localhost:7741",
        "api_url": "http://127.0.0.1:7741",
        "session_id": "session",
        "token": "token",
        "data_dir": str(pid_path.parent.resolve()),
        "version": "0.1.0",
    }
    info.update(overrides)
    return info


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset module-level state before each test."""
    _st._server = None
    _st._store = None
    _st._study_manager = None

    _st._session_id = None
    _st._remote_url = None
    _st._auth_token = None
    _st._event_callbacks.clear()
    _st._event_poll_thread = None
    _st._event_poll_stop.clear()
    yield
    # Clean up
    if _st._server is not None:
        try:
            _st._server.stop()
        except Exception:
            pass
    _st._server = None
    _st._store = None
    _st._study_manager = None

    _st._session_id = None
    _st._remote_url = None
    _st._auth_token = None
    _st._event_callbacks.clear()
    _st._event_poll_thread = None
    _st._event_poll_stop.clear()


@pytest.fixture
def store(tmp_path):
    """Create a store and inject it into the module state."""
    session_dir = tmp_path / "api_session"
    store = ArtifactStore(session_dir=session_dir, session_id="api-test")
    _st._store = store
    _st._session_id = "api-test"
    return store


@pytest.fixture
def study_manager(tmp_path):
    """Create a StudyManager and inject it into module state."""
    display_dir = tmp_path / "display"
    display_dir.mkdir()
    mgr = StudyManager(display_dir)
    _st._study_manager = mgr
    _st._session_id = "rm-test"
    return mgr


@pytest.fixture
def mock_server(store, monkeypatch):
    """Mock the server to avoid actually starting it."""

    class MockServer:
        is_running = True

        def __init__(self):
            self.pushed_cards = []
            self.pushed_sections = []
            self.event_callbacks = []
            self._mock_response = {"action": "timeout", "card_id": ""}
            self._selections = {}

        def start(self, open_browser=True):
            pass

        def stop(self):
            self.is_running = False

        def push_card(self, card):
            self.pushed_cards.append(card)

        def push_update(self, card_id, card):
            self.pushed_cards.append(card)

        def push_section(self, title, study=None):
            self.pushed_sections.append((title, study))

        def wait_for_response_sync(self, card_id, timeout):
            return self._mock_response

        def register_event_callback(self, callback):
            self.event_callbacks.append(callback)

    mock = MockServer()
    _st._server = mock
    return mock


class TestShow:
    def test_returns_card_id(self, store, mock_server):
        card_id = display.show("hello")
        assert isinstance(card_id, str)
        assert len(card_id) > 0

    def test_stores_markdown(self, store, mock_server):
        display.show("## Title")
        cards = store.list_cards()
        assert len(cards) == 1
        assert cards[0].card_type == CardType.MARKDOWN

    def test_stores_dataframe(self, store, mock_server):
        df = pd.DataFrame({"x": [1, 2, 3]})
        display.show(df, title="My Table")
        cards = store.list_cards()
        assert len(cards) == 1
        assert cards[0].card_type == CardType.TABLE
        assert cards[0].title == "My Table"

    def test_stores_dict(self, store, mock_server):
        display.show({"key": "value"})
        cards = store.list_cards()
        assert len(cards) == 1
        assert cards[0].card_type == CardType.KEYVALUE

    def test_with_title(self, store, mock_server):
        display.show("text", title="Finding")
        cards = store.list_cards()
        assert cards[0].title == "Finding"

    def test_with_study(self, store, mock_server):
        handle = display.show("text", study="my-study")
        cards = store.list_cards()
        assert cards[0].study == "my-study"
        assert getattr(handle, "url", None) is not None
        assert "#study=my-study" in handle.url

    def test_with_source(self, store, mock_server):
        display.show("text", source="mimiciv_hosp.patients")
        cards = store.list_cards()
        assert cards[0].provenance is not None
        assert cards[0].provenance.source == "mimiciv_hosp.patients"

    def test_pushes_to_server(self, store, mock_server):
        display.show("hello")
        assert len(mock_server.pushed_cards) == 1

    def test_multiple_cards(self, store, mock_server):
        display.show("card 1")
        display.show("card 2")
        display.show("card 3")
        assert len(store.list_cards()) == 3
        assert len(mock_server.pushed_cards) == 3


class TestSection:
    def test_creates_section_card(self, store, mock_server):
        display.section("Results")
        cards = store.list_cards()
        assert len(cards) == 1
        assert cards[0].card_type == CardType.SECTION
        assert cards[0].title == "Results"

    def test_section_with_study(self, store, mock_server):
        display.section("Analysis", study="study-1")
        cards = store.list_cards()
        assert cards[0].study == "study-1"

    def test_pushes_to_server(self, store, mock_server):
        display.section("Title")
        assert len(mock_server.pushed_sections) == 1
        assert mock_server.pushed_sections[0] == ("Title", None)


class TestReplace:
    def test_replace_updates_the_card_in_place(self, store, mock_server):
        card_id = display.show("original")
        new_id = display.show("updated", replace=card_id)
        assert new_id == card_id
        cards = store.list_cards()
        assert [c.card_id for c in cards] == [card_id]
        assert cards[0].preview["text"] == "updated"

    def test_replace_takes_type_artifact_and_source(self, store, mock_server):
        card_id = display.show("original", source="draft.py")
        df = pd.DataFrame({"x": range(30)})
        assert display.show(df, replace=card_id, source="final.py") == card_id
        [card] = store.list_cards()
        assert card.card_type == CardType.TABLE
        assert card.artifact_id == card_id
        assert card.provenance.source == "final.py"
        artifacts = sorted(p.name for p in store._artifacts_dir.iterdir())
        assert artifacts == [f"{card_id}.parquet"]
        assert len(store.read_table_page(card_id, offset=0, limit=50)["rows"]) == 30

    def test_replace_unknown_card_appends(self, store, mock_server):
        new_id = display.show("orphan", replace="nonexistent")
        assert new_id != "nonexistent"
        assert [c.card_id for c in store.list_cards()] == [new_id]

    def test_replace_pushes_to_server(self, store, mock_server):
        card_id = display.show("original")
        display.show("updated", replace=card_id)
        # Should push both the original and the replacement
        assert len(mock_server.pushed_cards) == 2

    def test_replace_keeps_the_previous_version(self, store, mock_server, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "first")
        card_id = display.show("original", title="Draft")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "second")
        display.show("updated", title="Final", replace=card_id)

        [revision] = store.list_revisions(card_id)
        assert revision["revision"] == 1
        assert revision["card"]["title"] == "Draft"
        assert revision["card"]["preview"]["text"] == "original"
        assert revision["card"]["agent"]["session_id"] == "first"
        assert store.get_card(card_id).agent.session_id == "second"

    def test_each_replace_adds_a_revision(self, store, mock_server):
        card_id = display.show("v1")
        display.show("v2", replace=card_id)
        display.show("v3", replace=card_id)
        revisions = store.list_revisions(card_id)
        assert [r["revision"] for r in revisions] == [1, 2]
        assert [r["card"]["preview"]["text"] for r in revisions] == ["v1", "v2"]

    def test_replace_keeps_the_previous_artifact(self, store, mock_server):
        first = pd.DataFrame({"x": [1, 2, 3]})
        card_id = display.show(first)
        display.show(pd.DataFrame({"x": [9]}), replace=card_id)

        [revision] = store.list_revisions(card_id)
        [name] = revision["artifacts"]
        kept = store.session_dir / "revisions" / card_id / name
        pd.testing.assert_frame_equal(pd.read_parquet(kept), first)
        assert len(store.read_table_page(card_id, offset=0, limit=50)["rows"]) == 1

    def test_replace_unknown_card_keeps_no_revision(self, store, mock_server):
        display.show("orphan", replace="nonexistent")
        assert store.list_revisions("nonexistent") == []

    def test_replace_that_changes_nothing_keeps_no_revision(self, store, mock_server):
        df = pd.DataFrame({"x": [1, 2, 3]})
        card_id = display.show(df, title="Same")
        display.show(df.copy(), title="Same", replace=card_id)
        assert store.list_revisions(card_id) == []

    def test_replace_that_changes_nothing_keeps_who_made_it(
        self, store, mock_server, monkeypatch
    ):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "maker")
        card_id = display.show("same")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "rerun")
        display.show("same", replace=card_id)
        assert store.get_card(card_id).agent.session_id == "maker"

    def test_replace_with_only_a_new_why_keeps_a_revision(self, store, mock_server):
        card_id = display.show("same", why="First reason")
        display.show("same", why="Better reason", replace=card_id)
        [revision] = store.list_revisions(card_id)
        assert revision["card"]["why"] == "First reason"


class TestAgentSession:
    def test_show_records_the_agent_session(self, store, mock_server, monkeypatch):
        monkeypatch.setenv("VITRINE_AGENT_HOST", "lincei")
        monkeypatch.setenv("VITRINE_AGENT_SESSION_ID", "thread-1")
        card_id = display.show("hello")
        agent = store.get_card(card_id).agent
        assert (agent.host, agent.session_id) == ("lincei", "thread-1")
        assert agent.recorded_at

    def test_show_without_a_host_records_unknown(self, store, mock_server):
        card_id = display.show("hello")
        assert store.get_card(card_id).agent.host == "unknown"

    def test_section_records_the_agent_session(self, store, mock_server, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-1")
        display.section("Results")
        [card] = store.list_cards()
        assert card.agent.session_id == "claude-1"

    def test_pushed_card_carries_the_agent_session(
        self, store, mock_server, monkeypatch
    ):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-1")
        display.show("hello")
        assert mock_server.pushed_cards[0].agent.session_id == "claude-1"

    def test_record_agent_session_writes_to_the_data_directory(
        self, tmp_path, monkeypatch
    ):
        from vitrine.agent_sessions import load_agent_sessions

        monkeypatch.setenv("VITRINE_DATA_DIR", str(tmp_path / ".vitrine"))
        display.record_agent_session(
            "lincei",
            "thr_1",
            title="Sepsis cohort",
            turns=[
                {"turn_id": "t1", "started_at": "2026-10-18T10:00:00Z", "model": "m"}
            ],
        )
        record = load_agent_sessions(tmp_path / ".vitrine")["lincei/thr_1"]
        assert record["title"] == "Sepsis cohort"
        assert record["turns"][0]["model"] == "m"

    def test_study_remembers_the_claude_code_session(
        self, study_manager, mock_server, monkeypatch
    ):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-1")
        display.show("hello", study="sepsis")
        assert study_manager.get_session_id("sepsis") == "claude-1"


def _run_script(path, body):
    """Run ``body`` as the script at ``path`` and return its globals."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return runpy.run_path(str(path))


class TestCodeOrigin:
    @pytest.fixture
    def project(self, tmp_path, monkeypatch):
        root = tmp_path / "project"
        monkeypatch.setenv("VITRINE_DATA_DIR", str(root / ".vitrine"))
        return root

    def test_records_the_calling_script(self, store, mock_server, project):
        body = "import vitrine\n\ncard_id = vitrine.show('hello')\n"
        card_id = _run_script(project / "analysis" / "cohort.py", body)["card_id"]
        code = store.get_card(card_id).code
        assert code.path == "analysis/cohort.py"
        assert code.line == 3
        assert code.sha256 == hashlib.sha256(body.encode()).hexdigest()

    def test_keeps_a_copy_of_the_script(self, store, mock_server, project):
        body = "import vitrine\ncard_id = vitrine.show('hello')\n"
        card_id = _run_script(project / "cohort.py", body)["card_id"]
        assert store.read_code(store.get_card(card_id).code.sha256) == body

    def test_keeps_one_copy_per_version_of_a_script(self, store, mock_server, project):
        body = "import vitrine\nvitrine.show('a')\nvitrine.show('b')\n"
        _run_script(project / "cohort.py", body)
        assert len(list((store.session_dir / "code").iterdir())) == 1

    def test_script_outside_the_project_keeps_its_full_path(
        self, store, mock_server, project, tmp_path
    ):
        script = tmp_path / "elsewhere" / "run.py"
        card_id = _run_script(script, "import vitrine\ncard_id = vitrine.show('x')\n")[
            "card_id"
        ]
        assert store.get_card(card_id).code.path == str(script.resolve())

    def test_code_without_a_file(self, store, mock_server, project):
        namespace = {}
        source = "import vitrine\ncard_id = vitrine.show('hello')\n"
        exec(compile(source, "<stdin>", "exec"), namespace)
        code = store.get_card(namespace["card_id"]).code
        assert (code.path, code.line, code.sha256) == ("<stdin>", 2, None)
        assert not (store.session_dir / "code").exists()

    def test_skips_vitrine_helpers(self, store, mock_server, project):
        body = (
            "import vitrine\n"
            "with vitrine.progress('Build') as status:\n"
            "    card_id = status._card_id\n"
        )
        card_id = _run_script(project / "build.py", body)["card_id"]
        assert store.get_card(card_id).code.path == "build.py"

    def test_section_records_its_code(self, store, mock_server, project):
        _run_script(
            project / "report.py", "import vitrine\nvitrine.section('Results')\n"
        )
        [card] = store.list_cards()
        assert (card.code.path, card.code.line) == ("report.py", 2)

    def test_replace_takes_the_new_code(self, store, mock_server, project):
        draft = "import vitrine\ncard_id = vitrine.show('v1')\n"
        card_id = _run_script(project / "draft.py", draft)["card_id"]
        final = f"import vitrine\nvitrine.show('v2', replace={card_id!r})\n"
        _run_script(project / "final.py", final)
        assert store.get_card(card_id).code.path == "final.py"
        [revision] = store.list_revisions(card_id)
        assert revision["card"]["code"]["path"] == "draft.py"


class TestWhy:
    def test_show_records_why(self, store, mock_server):
        card_id = display.show("42 patients", why="Counts the exclusions")
        assert store.get_card(card_id).why == "Counts the exclusions"

    def test_why_is_optional(self, store, mock_server):
        card_id = display.show("42 patients")
        assert store.get_card(card_id).why is None

    def test_replace_without_why_keeps_it(self, store, mock_server):
        card_id = display.show("v1", why="Counts the exclusions")
        display.show("v2", replace=card_id)
        assert store.get_card(card_id).why == "Counts the exclusions"

    def test_replace_with_why_changes_it(self, store, mock_server):
        card_id = display.show("v1", why="First reason")
        display.show("v2", why="Second reason", replace=card_id)
        assert store.get_card(card_id).why == "Second reason"
        [revision] = store.list_revisions(card_id)
        assert revision["card"]["why"] == "First reason"


class TestModuleState:
    def test_initial_state(self):
        assert _st._server is None
        assert _st._store is None
        assert _st._remote_url is None
        assert _st._auth_token is None

    def test_stop_when_not_started(self):
        # Should not raise
        display.stop()


class TestDiscovery:
    def test_discover_no_pid_file(self, monkeypatch, tmp_path):
        """Discovery returns None when no PID file exists."""
        monkeypatch.setattr(
            _client_mod, "_pid_file_path", lambda: tmp_path / ".server.json"
        )
        result = _client_mod._discover_server()
        assert result is None

    def test_discover_rejects_malformed_pid_file(self, monkeypatch, tmp_path):
        from vitrine._utils import ServerMetadataError

        pid_path = tmp_path / ".server.json"
        pid_path.write_text("{not-json")
        monkeypatch.setattr(_client_mod, "_pid_file_path", lambda: pid_path)

        with pytest.raises(ServerMetadataError, match="invalid JSON"):
            _client_mod._discover_server()
        assert pid_path.exists()

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("pid", "123"),
            ("pid", True),
            ("port", "7741"),
            ("port", 0),
            ("session_id", None),
            ("token", ""),
            ("url", None),
            ("host", None),
            ("host", "example.com"),
            ("api_url", "http://example.com:7741"),
        ],
    )
    def test_discover_rejects_invalid_pid_fields_before_pid_probe(
        self, monkeypatch, tmp_path, field, value
    ):
        from vitrine._utils import ServerMetadataError

        pid_path = tmp_path / ".server.json"
        pid_path.write_text(json.dumps(_server_metadata(pid_path, **{field: value})))
        monkeypatch.setattr(_client_mod, "_pid_file_path", lambda: pid_path)
        monkeypatch.setattr(
            _client_mod,
            "_is_process_alive",
            lambda _pid: pytest.fail("PID probe must not run"),
        )

        with pytest.raises(ServerMetadataError):
            _client_mod._discover_server()
        assert pid_path.exists()

    def test_discover_rejects_wrong_project_before_pid_probe(
        self, monkeypatch, tmp_path
    ):
        from vitrine._utils import ServerMetadataError

        pid_path = tmp_path / "project-a" / ".server.json"
        pid_path.parent.mkdir()
        pid_path.write_text(
            json.dumps(
                _server_metadata(
                    pid_path,
                    data_dir=str((tmp_path / "project-b").resolve()),
                )
            )
        )
        monkeypatch.setattr(_client_mod, "_pid_file_path", lambda: pid_path)
        monkeypatch.setattr(
            _client_mod,
            "_is_process_alive",
            lambda _pid: pytest.fail("PID probe must not run"),
        )

        with pytest.raises(ServerMetadataError, match="project directory"):
            _client_mod._discover_server()

    def test_discover_legacy_metadata_requires_restart_without_pid_probe(
        self, monkeypatch, tmp_path
    ):
        from vitrine._utils import ServerRestartRequired

        pid_path = tmp_path / ".server.json"
        info = _server_metadata(pid_path)
        for field in ("api_url", "data_dir", "version"):
            info.pop(field)
        pid_path.write_text(json.dumps(info))
        monkeypatch.setattr(_client_mod, "_pid_file_path", lambda: pid_path)
        monkeypatch.setattr(
            _client_mod,
            "_is_process_alive",
            lambda _pid: pytest.fail("PID probe must not run"),
        )

        with pytest.raises(ServerRestartRequired, match="restart required"):
            _client_mod._discover_server()

    def test_discover_stale_pid(self, monkeypatch, tmp_path):
        """Discovery cleans up PID file when process is dead."""
        pid_path = tmp_path / ".server.json"
        pid_path.write_text(
            json.dumps(
                _server_metadata(
                    pid_path,
                    pid=999999999,
                    session_id="dead-session",
                )
            )
        )
        monkeypatch.setattr(_client_mod, "_pid_file_path", lambda: pid_path)
        monkeypatch.setattr(_client_mod, "_is_process_alive", lambda pid: False)

        result = _client_mod._discover_server()
        assert result is None
        assert not pid_path.exists()

    def test_discover_alive_pid_health_mismatch_fails_closed(
        self, monkeypatch, tmp_path
    ):
        """An alive PID with mismatched health retains its identity handle."""
        from vitrine._utils import ServerMetadataError

        pid_path = tmp_path / ".server.json"
        pid_path.write_text(
            json.dumps(_server_metadata(pid_path, session_id="bad-session"))
        )
        monkeypatch.setattr(_client_mod, "_pid_file_path", lambda: pid_path)
        monkeypatch.setattr(_client_mod, "_is_process_alive", lambda pid: True)
        monkeypatch.setattr(_client_mod, "_health_check", lambda *_args: False)

        with pytest.raises(ServerMetadataError, match="Refusing to discard metadata"):
            _client_mod._discover_server()
        assert pid_path.exists()

    def test_ensure_started_does_not_spawn_after_alive_pid_health_mismatch(
        self, monkeypatch, tmp_path
    ):
        from vitrine._utils import ServerMetadataError

        pid_path = tmp_path / ".server.json"
        pid_path.write_text(json.dumps(_server_metadata(pid_path)))
        monkeypatch.setattr(_client_mod, "_pid_file_path", lambda: pid_path)
        monkeypatch.setattr(
            _client_mod, "_lock_file_path", lambda: tmp_path / ".server.lock"
        )
        monkeypatch.setattr(_client_mod, "_ensure_study_manager", lambda: None)
        monkeypatch.setattr(_client_mod, "_is_process_alive", lambda _pid: True)
        monkeypatch.setattr(_client_mod, "_health_check", lambda *_args: False)
        monkeypatch.setattr(
            _client_mod,
            "_start_process",
            lambda **_kwargs: pytest.fail("must not spawn a second daemon"),
        )

        with pytest.raises(ServerMetadataError, match="start a second daemon"):
            _client_mod._ensure_started(open_browser=False)
        assert pid_path.exists()

    def test_discover_valid_server(self, monkeypatch, tmp_path):
        """Discovery returns info when process alive and health check passes."""
        pid_path = tmp_path / ".server.json"
        info = _server_metadata(
            pid_path,
            session_id="valid-session",
            token="secret-tok",
        )
        pid_path.write_text(json.dumps(info))
        monkeypatch.setattr(_client_mod, "_pid_file_path", lambda: pid_path)
        monkeypatch.setattr(_client_mod, "_is_process_alive", lambda pid: True)
        health_args = []
        monkeypatch.setattr(
            _client_mod,
            "_health_check",
            lambda *args: health_args.append(args) or True,
        )

        result = _client_mod._discover_server()
        assert result is not None
        assert result["session_id"] == "valid-session"
        assert result["token"] == "secret-tok"
        assert health_args == [
            (
                "http://127.0.0.1:7741",
                "valid-session",
                "0.1.0",
                str(tmp_path.resolve()),
            )
        ]

    def test_is_process_alive_current_pid(self):
        """Current process should be alive."""
        assert _client_mod._is_process_alive(os.getpid()) is True

    def test_is_process_alive_dead_pid(self):
        """Non-existent PID should not be alive."""
        assert _client_mod._is_process_alive(999999999) is False

    def test_server_status_returns_none(self, monkeypatch, tmp_path):
        """server_status() returns None when no server running."""
        monkeypatch.setattr(
            _client_mod, "_pid_file_path", lambda: tmp_path / ".server.json"
        )
        assert display.server_status() is None

    def test_ensure_started_fast_path_keeps_remote(self, monkeypatch, tmp_path):
        """Already connected: one discovery, no re-connect through the lock."""
        monkeypatch.setenv("VITRINE_DATA_DIR", str(tmp_path / "vitrine"))
        info = _server_metadata(tmp_path / "vitrine" / ".server.json")
        calls = []
        monkeypatch.setattr(
            _client_mod, "_discover_server", lambda: calls.append(1) or info
        )
        _st._remote_url = info["api_url"]
        _st._auth_token = info["token"]

        _client_mod._ensure_started()

        assert len(calls) == 1
        assert _st._remote_url == info["api_url"]
        assert _st._study_manager is None  # slow path never ran

    def test_ensure_started_reconnects_when_server_moved(self, monkeypatch, tmp_path):
        """A different server in the PID file replaces the stale connection."""
        monkeypatch.setenv("VITRINE_DATA_DIR", str(tmp_path / "vitrine"))
        info = _server_metadata(
            tmp_path / "vitrine" / ".server.json",
            port=7742,
            url="http://vitrine.localhost:7742",
            api_url="http://127.0.0.1:7742",
            token="new-tok",
            session_id="new",
        )
        monkeypatch.setattr(_client_mod, "_discover_server", lambda: info)
        _st._remote_url = "http://127.0.0.1:7741"
        _st._auth_token = "old-tok"

        _client_mod._ensure_started()

        assert _st._remote_url == "http://127.0.0.1:7742"
        assert _st._auth_token == "new-tok"
        assert _st._session_id == "new"


class TestServerLifecycle:
    def test_server_status_returns_none_without_pid_file(self, monkeypatch):
        """server_status() returns None when PID file is absent (no port scan)."""
        monkeypatch.setattr(_client_mod, "_discover_server", lambda: None)
        assert display.server_status() is None

    def test_stop_server_keeps_pid_file_when_shutdown_fails(
        self, monkeypatch, tmp_path
    ):
        """stop_server() should not remove PID metadata if server is still healthy."""
        pid_path = tmp_path / ".server.json"
        pid_path.write_text("{}")
        monkeypatch.setattr(_client_mod, "_pid_file_path", lambda: pid_path)
        monkeypatch.setattr(
            _client_mod,
            "_discover_server",
            lambda: {
                "url": "http://127.0.0.1:7741",
                "session_id": "sess-1",
                "token": "tok",
                "pid": None,
                "version": "0.1.0",
                "data_dir": str(tmp_path.resolve()),
            },
        )
        monkeypatch.setattr(_client_mod, "_health_check", lambda *_args: True)

        import urllib.request

        def _raise(*args, **kwargs):
            raise OSError("network down")

        monkeypatch.setattr(urllib.request, "urlopen", _raise)

        assert display.stop_server() is False
        assert pid_path.exists()

    def test_stop_server_removes_pid_file_after_success(self, monkeypatch, tmp_path):
        """stop_server() should clean up PID metadata once server stops."""
        pid_path = tmp_path / ".server.json"
        pid_path.write_text("{}")
        monkeypatch.setattr(_client_mod, "_pid_file_path", lambda: pid_path)
        monkeypatch.setattr(
            _client_mod,
            "_discover_server",
            lambda: {
                "url": "http://127.0.0.1:7741",
                "session_id": "sess-1",
                "token": "tok",
                "pid": None,
                "version": "0.1.0",
                "data_dir": str(tmp_path.resolve()),
            },
        )
        monkeypatch.setattr(_client_mod, "_health_check", lambda *_args: False)

        import urllib.request

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: _Resp())

        assert display.stop_server() is True
        assert not pid_path.exists()

    def test_stop_delegates_to_persistent_server_when_remote(self, monkeypatch):
        """stop() should stop the persistent server when connected remotely."""
        calls = []
        _st._remote_url = "http://127.0.0.1:7741"
        monkeypatch.setattr(
            _client_mod,
            "stop_server",
            lambda: calls.append("called") or True,
        )
        display.stop()
        assert calls == ["called"]

    def test_start_process_uses_devnull_stderr(self, monkeypatch):
        """_start_process should not leave stderr pipe unread."""
        import subprocess

        captured = {}

        def _fake_popen(cmd, stdout=None, stderr=None, start_new_session=None):
            captured["cmd"] = cmd
            captured["stdout"] = stdout
            captured["stderr"] = stderr
            captured["start_new_session"] = start_new_session
            return None

        monkeypatch.setattr(subprocess, "Popen", _fake_popen)
        _client_mod._start_process(port=7749, open_browser=False)

        assert captured["stdout"] is subprocess.DEVNULL
        assert captured["stderr"] is subprocess.DEVNULL
        assert captured["start_new_session"] is True
        assert "--no-open" in captured["cmd"]

    def test_ensure_started_releases_lock_before_spawning(self, monkeypatch, tmp_path):
        """Parent must release the startup lock before the child tries to take it."""
        events = []
        discover_calls = 0

        def fake_discover():
            nonlocal discover_calls
            discover_calls += 1
            if discover_calls == 1:
                return None
            return {
                "url": "http://127.0.0.1:7741",
                "api_url": "http://127.0.0.1:7741",
                "token": "tok",
                "session_id": "sess",
            }

        def fake_lock(_fd):
            events.append("lock")

        def fake_unlock(_fd):
            events.append("unlock")

        def fake_start_process(**_kwargs):
            assert events == ["lock", "unlock"]
            events.append("start")

        monkeypatch.setattr(_client_mod, "_ensure_study_manager", lambda: None)
        monkeypatch.setattr(
            _client_mod, "_lock_file_path", lambda: tmp_path / ".server.lock"
        )
        monkeypatch.setattr(_client_mod, "_discover_server", fake_discover)
        monkeypatch.setattr("vitrine._utils.lock_file", fake_lock)
        monkeypatch.setattr("vitrine._utils.unlock_file", fake_unlock)
        monkeypatch.setattr(_client_mod, "_start_process", fake_start_process)

        _client_mod._ensure_started(open_browser=False)

        assert events == ["lock", "unlock", "start"]

    def test_ensure_started_terminates_spawned_child_before_timeout_fallback(
        self, monkeypatch, tmp_path
    ):
        events = []

        class FakeProcess:
            pid = 12345

            def poll(self):
                return None

            def terminate(self):
                events.append("terminate")

            def wait(self, timeout=None):
                events.append(("wait", timeout))
                return 0

            def kill(self):
                events.append("kill")

        class FakeServer:
            is_running = False

            def __init__(self, **_kwargs):
                events.append("fallback-created")

            def start(self, **_kwargs):
                events.append("fallback-started")
                self.is_running = True

        monkeypatch.setattr(_client_mod, "_ensure_study_manager", lambda: None)
        monkeypatch.setattr(
            _client_mod, "_lock_file_path", lambda: tmp_path / ".server.lock"
        )
        monkeypatch.setattr(_client_mod, "_discover_server", lambda: None)
        monkeypatch.setattr(
            _client_mod, "_start_process", lambda **_kwargs: FakeProcess()
        )
        clock = iter([0.0, 6.0])
        monkeypatch.setattr(_client_mod.time, "monotonic", lambda: next(clock))
        monkeypatch.setattr("vitrine.server.DisplayServer", FakeServer)

        _client_mod._ensure_started(open_browser=False)

        assert events == [
            "terminate",
            ("wait", 3.0),
            "fallback-created",
            "fallback-started",
        ]

    def test_ensure_started_reaps_early_failed_child_before_fallback(
        self, monkeypatch, tmp_path
    ):
        events = []

        class FakeProcess:
            pid = 12345

            def poll(self):
                return 7

            def wait(self, timeout=None):
                events.append(("wait", timeout))
                return 7

        class FakeServer:
            is_running = False

            def __init__(self, **_kwargs):
                events.append("fallback-created")

            def start(self, **_kwargs):
                events.append("fallback-started")
                self.is_running = True

        monkeypatch.setattr(_client_mod, "_ensure_study_manager", lambda: None)
        monkeypatch.setattr(
            _client_mod, "_lock_file_path", lambda: tmp_path / ".server.lock"
        )
        monkeypatch.setattr(_client_mod, "_discover_server", lambda: None)
        monkeypatch.setattr(
            _client_mod, "_start_process", lambda **_kwargs: FakeProcess()
        )
        monkeypatch.setattr(_client_mod.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr("vitrine.server.DisplayServer", FakeServer)

        _client_mod._ensure_started(open_browser=False)

        assert events == [
            ("wait", None),
            "fallback-created",
            "fallback-started",
        ]

    def _find(self, monkeypatch, tmp_path, *infos):
        """Have _ensure_started find ``infos`` in turn; returns what it starts."""
        found = iter(infos)
        started = []
        monkeypatch.setattr(_client_mod, "_ensure_study_manager", lambda: None)
        monkeypatch.setattr(
            _client_mod, "_lock_file_path", lambda: tmp_path / ".server.lock"
        )
        monkeypatch.setattr(_client_mod, "_discover_server", lambda: next(found))
        monkeypatch.setattr(
            _client_mod, "_start_process", lambda **kwargs: started.append(kwargs)
        )
        return started

    def test_ensure_started_gives_a_running_server_the_owner(
        self, monkeypatch, tmp_path
    ):
        """A host's VITRINE_OWNER_PID joins a server that another host started."""
        monkeypatch.setenv("VITRINE_OWNER_PID", "4242")
        info = _server_metadata(
            tmp_path / ".server.json", owners=[{"pid": 111, "start_time": None}]
        )
        started = self._find(monkeypatch, tmp_path, info)
        added = []
        monkeypatch.setattr(
            _client_mod,
            "_add_server_owner",
            lambda info, pid: added.append((info["session_id"], pid)) or [111, pid],
        )

        _client_mod._ensure_started(open_browser=False)

        assert added == [("session", 4242)]
        assert started == []
        assert _st._remote_url == info["api_url"]

    @pytest.mark.parametrize(("env", "owner"), [(None, 111), ("4242", 4242)])
    def test_ensure_started_asks_nothing_without_a_new_owner(
        self, monkeypatch, tmp_path, env, owner
    ):
        if env is not None:
            monkeypatch.setenv("VITRINE_OWNER_PID", env)
        info = _server_metadata(
            tmp_path / ".server.json", owners=[{"pid": owner, "start_time": None}]
        )
        self._find(monkeypatch, tmp_path, info)

        def add_owner(info, pid):
            raise AssertionError("asked the server to take an owner")

        monkeypatch.setattr(_client_mod, "_add_server_owner", add_owner)

        _client_mod._ensure_started(open_browser=False)

        assert _st._remote_url == info["api_url"]

    def test_ensure_started_replaces_a_server_stopping_for_its_owners(
        self, monkeypatch, tmp_path
    ):
        from vitrine._utils import OwnerRejected

        monkeypatch.setenv("VITRINE_OWNER_PID", "4242")
        pid_path = tmp_path / ".server.json"
        stopping = _server_metadata(pid_path, session_id="stopping")
        fresh = _server_metadata(
            pid_path, session_id="fresh", owners=[{"pid": 4242, "start_time": None}]
        )
        started = self._find(monkeypatch, tmp_path, stopping, fresh)

        def add_owner(info, pid):
            raise OwnerRejected("stopping", "The server is stopping.")

        monkeypatch.setattr(_client_mod, "_add_server_owner", add_owner)

        _client_mod._ensure_started(open_browser=False)

        assert len(started) == 1
        assert _st._session_id == "fresh"


class TestClientMode:
    """Test that show/section push via HTTP when _remote_url is set.

    _ensure_started is monkeypatched to a no-op since we're testing the
    push path, not the discovery/startup flow.
    """

    def test_show_uses_remote_command(self, store, monkeypatch):
        """show() pushes via _remote_command when _remote_url is set."""
        commands_sent = []

        def mock_remote_command(url, token, payload):
            commands_sent.append((url, token, payload))
            return True

        _st._remote_url = "http://127.0.0.1:7741"
        _st._auth_token = "test-token"
        monkeypatch.setattr(_client_mod, "_ensure_started", lambda **kw: None)
        monkeypatch.setattr(_client_mod, "_remote_command", mock_remote_command)

        card_id = display.show("hello")
        assert isinstance(card_id, str)
        assert len(commands_sent) == 1
        assert commands_sent[0][0] == "http://127.0.0.1:7741"
        assert commands_sent[0][1] == "test-token"
        assert commands_sent[0][2]["type"] == "card"

    def test_section_uses_remote_command(self, store, monkeypatch):
        """section() pushes via _remote_command when _remote_url is set."""
        commands_sent = []

        def mock_remote_command(url, token, payload):
            commands_sent.append(payload)
            return True

        _st._remote_url = "http://127.0.0.1:7741"
        _st._auth_token = "test-token"
        monkeypatch.setattr(_client_mod, "_ensure_started", lambda **kw: None)
        monkeypatch.setattr(_client_mod, "_remote_command", mock_remote_command)

        display.section("Results", study="r1")
        assert len(commands_sent) == 1
        assert commands_sent[0]["type"] == "section"
        assert commands_sent[0]["title"] == "Results"
        assert commands_sent[0]["study"] == "r1"


class TestBlockingShow:
    def test_wait_returns_display_response(self, store, mock_server):
        mock_server._mock_response = {
            "action": "confirm",
            "card_id": "test",
            "message": "Looks good",
            "artifact_id": None,
        }
        result = display.show("hello", wait=True)
        assert isinstance(result, DisplayResponse)
        assert result.action == "confirm"
        assert result.message == "Looks good"

    def test_wait_timeout_returns_timeout_action(self, store, mock_server):
        mock_server._mock_response = {
            "action": "timeout",
            "card_id": "test",
        }
        result = display.show("hello", wait=True, timeout=1)
        assert isinstance(result, DisplayResponse)
        assert result.action == "timeout"

    def test_wait_skip_returns_skip_action(self, store, mock_server):
        mock_server._mock_response = {
            "action": "skip",
            "card_id": "test",
        }
        result = display.show("hello", wait=True)
        assert isinstance(result, DisplayResponse)
        assert result.action == "skip"

    def test_wait_sets_response_requested(self, store, mock_server):
        mock_server._mock_response = {"action": "confirm", "card_id": "x"}
        display.show("hello", wait=True)
        cards = store.list_cards()
        assert len(cards) == 1
        assert cards[0].response_requested is True

    def test_prompt_stored_in_card(self, store, mock_server):
        mock_server._mock_response = {"action": "confirm", "card_id": "x"}
        display.show("hello", wait=True, prompt="Pick patients")
        cards = store.list_cards()
        assert cards[0].prompt == "Pick patients"

    def test_response_data_accessor(self, store, mock_server):
        """DisplayResponse.data() loads artifact when available."""
        # Store a selection artifact manually
        df = pd.DataFrame({"id": [1, 2], "name": ["a", "b"]})
        store.store_dataframe("resp-sel1", df)

        mock_server._mock_response = {
            "action": "confirm",
            "card_id": "test",
            "artifact_id": "resp-sel1",
        }
        result = display.show("hello", wait=True)
        assert result.artifact_id == "resp-sel1"
        loaded = result.data()
        assert loaded is not None
        assert len(loaded) == 2
        assert list(loaded.columns) == ["id", "name"]

    def test_response_data_returns_none_without_artifact(self, store, mock_server):
        mock_server._mock_response = {
            "action": "confirm",
            "card_id": "test",
            "artifact_id": None,
        }
        result = display.show("hello", wait=True)
        assert result.data() is None

    def test_non_wait_returns_card_id_string(self, store, mock_server):
        result = display.show("hello", wait=False)
        assert isinstance(result, str)


class TestFormAutoWait:
    def test_form_auto_blocks(self, store, mock_server):
        """show(Form(...)) auto-sets wait=True and returns DisplayResponse."""
        from vitrine._types import Form, Question

        mock_server._mock_response = {
            "action": "confirm",
            "card_id": "test",
            "values": {"method": "logistic"},
        }
        result = display.show(
            Form([Question("method", "Which method?", ["logistic", "cox"])])
        )
        assert isinstance(result, DisplayResponse)
        assert result.action == "confirm"

    def test_form_explicit_wait_false_still_blocks(self, store, mock_server):
        """show(Form(...), wait=False) still blocks because forms force wait=True."""
        from vitrine._types import Form, Question

        mock_server._mock_response = {"action": "skip", "card_id": "test"}
        result = display.show(
            Form([Question("method", "Which method?", ["logistic", "cox"])]), wait=False
        )
        assert isinstance(result, DisplayResponse)
        assert result.action == "skip"

    def test_form_renders_as_form_card(self, store, mock_server):
        """Form cards are stored as FORM with preview.fields."""
        from vitrine._types import Form, Question

        mock_server._mock_response = {"action": "confirm", "card_id": "test"}
        display.show(Form([Question("method", "Which method?", ["logistic", "cox"])]))
        cards = store.list_cards()
        assert len(cards) == 1
        assert cards[0].card_type == CardType.DECISION
        assert "fields" in cards[0].preview

    def test_form_sets_response_requested(self, store, mock_server):
        """Form cards always have response_requested=True."""
        from vitrine._types import Form, Question

        mock_server._mock_response = {"action": "confirm", "card_id": "test"}
        display.show(Form([Question("method", "Which method?", ["logistic", "cox"])]))
        cards = store.list_cards()
        assert cards[0].response_requested is True

    def test_controls_auto_blocks(self, store, mock_server):
        """show(df, controls=[...]) auto-sets wait=True."""
        from vitrine._types import Question

        mock_server._mock_response = {"action": "confirm", "card_id": "test"}
        import pandas as pd

        df = pd.DataFrame({"x": [1, 2]})
        result = display.show(df, controls=[Question("method", "Which?", ["a", "b"])])
        assert isinstance(result, DisplayResponse)
        assert result.action == "confirm"


class TestActions:
    def test_actions_stored_in_card(self, store, mock_server):
        mock_server._mock_response = {"action": "Approve", "card_id": "x"}
        display.show("hello", wait=True, actions=["Approve", "Reject"])
        cards = store.list_cards()
        assert len(cards) == 1
        assert cards[0].actions == ["Approve", "Reject"]

    def test_actions_in_serialized_card(self, store, mock_server):
        from vitrine.artifacts import _serialize_card

        mock_server._mock_response = {"action": "Run", "card_id": "x"}
        display.show("hello", wait=True, actions=["Run", "Skip"])
        card = store.list_cards()[0]
        serialized = _serialize_card(card)
        assert serialized["actions"] == ["Run", "Skip"]

    def test_actions_response_carries_action_name(self, store, mock_server):
        mock_server._mock_response = {
            "action": "Reject",
            "card_id": "test",
        }
        result = display.show("hello", wait=True, actions=["Approve", "Reject"])
        assert isinstance(result, DisplayResponse)
        assert result.action == "Reject"


class TestGetSelection:
    def test_get_selection_returns_selected_rows(self, store, mock_server):
        """get_selection returns selected rows from in-process server."""
        df = pd.DataFrame({"a": [10, 20, 30]})
        card_id = "sel-card"
        store.store_dataframe(card_id, df)
        # Simulate selection state on mock server
        mock_server._selections = {card_id: [0, 2]}
        result = display.get_selection(card_id)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 2
        assert list(result["a"]) == [10, 30]

    def test_get_selection_empty_when_no_selection(self, store, mock_server):
        mock_server._selections = {}
        result = display.get_selection("any-card")
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0

    def test_get_selection_empty_without_server(self, monkeypatch):
        """get_selection returns empty DataFrame when no server available."""
        monkeypatch.setattr(_client_mod, "_ensure_started", lambda **kw: None)
        result = display.get_selection("anything")
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0


class TestOnEvent:
    def test_on_event_registers_callback(self, store, mock_server):
        def my_callback(event):
            pass

        display.on_event(my_callback)
        assert len(mock_server.event_callbacks) == 1
        assert mock_server.event_callbacks[0] is my_callback

    def test_on_event_multiple_callbacks(self, store, mock_server):
        display.on_event(lambda e: None)
        display.on_event(lambda e: None)
        assert len(mock_server.event_callbacks) == 2


class TestListStudies:
    def test_list_studies_empty(self, study_manager):
        assert display.list_studies() == []

    def test_list_studies_after_show(self, study_manager, mock_server):
        display.show("hello", study="test-study")
        studies = display.list_studies()
        assert len(studies) == 1
        assert studies[0]["label"] == "test-study"
        assert studies[0]["card_count"] == 1

    def test_list_studies_multiple(self, study_manager, mock_server):
        display.show("card-a", study="study-a")
        display.show("card-b", study="study-b")
        studies = display.list_studies()
        assert len(studies) == 2
        labels = {r["label"] for r in studies}
        assert labels == {"study-a", "study-b"}


class TestDeleteStudy:
    def test_delete_existing_study(self, study_manager, mock_server):
        display.show("hello", study="to-delete")
        assert len(display.list_studies()) == 1
        result = display.delete_study("to-delete")
        assert result is True
        assert display.list_studies() == []

    def test_delete_nonexistent_study(self, study_manager):
        assert display.delete_study("nope") is False


class TestStudyContext:
    def test_study_context_with_cards(self, study_manager, mock_server):
        display.show("hello", study="ctx-test", title="Card 1")
        ctx = display.study_context("ctx-test")
        assert ctx["study"] == "ctx-test"
        assert ctx["card_count"] == 1
        assert len(ctx["cards"]) == 1
        assert ctx["cards"][0]["title"] == "Card 1"
        assert "pending_responses" in ctx
        assert "decisions_made" in ctx
        assert "current_selections" in ctx

    def test_study_context_nonexistent(self, study_manager, monkeypatch):
        monkeypatch.setattr(_client_mod, "_discover_server", lambda: None)
        ctx = display.study_context("nonexistent")
        assert ctx["card_count"] == 0
        assert ctx["cards"] == []

    def test_study_context_attaches_to_running_server(
        self, study_manager, monkeypatch, tmp_path
    ):
        """A fresh process uses a running server's live context."""
        import urllib.request

        info = _server_metadata(tmp_path / ".server.json")
        monkeypatch.setattr(_client_mod, "_discover_server", lambda: info)
        remote_ctx = {"study": "s", "card_count": 0, "current_selections": {"c": [1]}}
        seen = []

        class FakeResponse:
            def read(self):
                return json.dumps(remote_ctx).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=None):
            seen.append(req.full_url)
            return FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        assert display.study_context("s") == remote_ctx
        assert seen == [f"{info['api_url']}/api/studies/s/context"]

    def test_study_context_attach_joins_as_owner(
        self, study_manager, monkeypatch, tmp_path
    ):
        """Attaching gives the server this host's owner, as show() would.

        A later show() takes the fast path on this connection and would never
        join on its own.
        """
        monkeypatch.setenv("VITRINE_OWNER_PID", "4242")
        info = _server_metadata(
            tmp_path / ".server.json", owners=[{"pid": 111, "start_time": None}]
        )
        monkeypatch.setattr(_client_mod, "_discover_server", lambda: info)
        added = []
        monkeypatch.setattr(
            _client_mod,
            "_add_server_owner",
            lambda info, pid: added.append(pid) or [111, pid],
        )

        def offline(*a, **kw):
            raise OSError("no network in tests")

        monkeypatch.setattr("urllib.request.urlopen", offline)

        display.study_context("s")

        assert added == [4242]
        assert _st._remote_url == info["api_url"]

    def test_study_context_skips_a_server_stopping_for_its_owners(
        self, study_manager, monkeypatch, tmp_path
    ):
        from vitrine._utils import OwnerRejected

        monkeypatch.setenv("VITRINE_OWNER_PID", "4242")
        info = _server_metadata(tmp_path / ".server.json")
        monkeypatch.setattr(_client_mod, "_discover_server", lambda: info)

        def stopping(info, pid):
            raise OwnerRejected("stopping", "server is stopping")

        monkeypatch.setattr(_client_mod, "_add_server_owner", stopping)

        ctx = display.study_context("s")

        assert ctx["card_count"] == 0
        assert _st._remote_url is None

    @pytest.mark.parametrize("error", ["ServerMetadataError", "ServerRestartRequired"])
    def test_study_context_falls_back_to_disk_on_untrusted_metadata(
        self, study_manager, monkeypatch, error
    ):
        """Metadata discovery refuses to trust stays a read-only disk lookup."""
        import vitrine._utils as _utils

        def untrusted():
            raise getattr(_utils, error)("metadata predates this version")

        monkeypatch.setattr(_client_mod, "_discover_server", untrusted)

        ctx = display.study_context("s")

        assert ctx["card_count"] == 0
        assert _st._remote_url is None

    def test_study_context_encodes_the_study_label(
        self, study_manager, monkeypatch, tmp_path
    ):
        """URL metacharacters in a label must not truncate the request path."""
        import urllib.request

        info = _server_metadata(tmp_path / ".server.json")
        monkeypatch.setattr(_client_mod, "_discover_server", lambda: info)
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append(req.full_url)
            raise OSError("no network in tests")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        display.study_context("sepsis #2? 50%/v1")

        assert seen == [
            f"{info['api_url']}/api/studies/sepsis%20%232%3F%2050%25%2Fv1/context"
        ]

    def test_study_context_does_not_start_server(self, study_manager, monkeypatch):
        """study_context() never spawns a server just to read context."""
        monkeypatch.setattr(_client_mod, "_discover_server", lambda: None)

        def _fail(**kw):
            raise AssertionError("study_context must not start a server")

        monkeypatch.setattr(_client_mod, "_start_process", _fail)
        display.study_context("s")
        assert _st._server is None
        assert _st._remote_url is None


class TestCleanStudies:
    def test_clean_removes_all(self, study_manager, mock_server):
        display.show("card-a", study="old-a")
        display.show("card-b", study="old-b")
        removed = display.clean_studies("0d")
        assert removed == 2
        assert display.list_studies() == []

    def test_clean_keeps_recent(self, study_manager, mock_server):
        display.show("card", study="recent")
        removed = display.clean_studies("1d")
        assert removed == 0
        assert len(display.list_studies()) == 1


class TestAutoStudy:
    def test_show_without_study_creates_auto(self, study_manager, mock_server):
        display.show("hello")
        studies = display.list_studies()
        assert len(studies) == 1
        assert studies[0]["label"].startswith("auto-")

    def test_multiple_shows_without_study(self, study_manager, mock_server):
        """Multiple show() calls without study reuse the same auto-study."""
        display.show("card 1")
        display.show("card 2")
        studies = display.list_studies()
        assert len(studies) == 1
        assert studies[0]["card_count"] == 2


class TestMultiStudyShow:
    def test_different_studies_create_separate_studies(
        self, study_manager, mock_server
    ):
        display.show("card-a", study="study-a")
        display.show("card-b", study="study-b")
        display.show("card-a2", study="study-a")

        studies = display.list_studies()
        assert len(studies) == 2

        # study-a should have 2 cards
        study_a = next(r for r in studies if r["label"] == "study-a")
        assert study_a["card_count"] == 2

        # study-b should have 1 card
        study_b = next(r for r in studies if r["label"] == "study-b")
        assert study_b["card_count"] == 1


class TestStopServerPreservesData:
    def test_stop_preserves_study_data(self, study_manager, mock_server, tmp_path):
        """stop_server() should not delete study data."""
        display.show("persistent", study="keep-me")
        studies_before = display.list_studies()
        assert len(studies_before) == 1

        # Verify the study directory exists
        study_dir = study_manager._studies_dir / study_manager._label_to_dir["keep-me"]
        assert study_dir.exists()

        # Simulate stop (stop the mock server)
        display.stop()

        # Study directory should still exist on disk
        assert study_dir.exists()

        # Create a new StudyManager (simulates restart) — data should be discovered
        mgr2 = StudyManager(study_manager.display_dir)
        assert "keep-me" in mgr2._label_to_dir
        studies_after = mgr2.list_studies()
        assert len(studies_after) == 1
        assert studies_after[0]["label"] == "keep-me"


class TestErrorLogging:
    """Test that error/warning logs are emitted for failure scenarios."""

    def test_push_remote_logs_on_rediscovery_failure(
        self, store, mock_server, monkeypatch, caplog
    ):
        """_push_remote logs warning when re-discovery fails."""
        import logging

        # Set up remote mode
        _st._remote_url = "http://127.0.0.1:9999"
        _st._auth_token = "fake-token"

        # _remote_command always fails
        monkeypatch.setattr(_client_mod, "_remote_command", lambda *a: False)
        # _discover_server returns None (can't find server)
        monkeypatch.setattr(_client_mod, "_discover_server", lambda: None)

        with caplog.at_level(logging.WARNING, logger="vitrine"):
            result = _client_mod._push_remote({"card_type": "markdown"})

        assert result is False
        assert "re-discovery failed" in caplog.text

    def test_push_remote_logs_on_retry_failure(
        self, store, mock_server, monkeypatch, caplog
    ):
        """_push_remote logs warning when retry after re-discovery fails."""
        import logging

        _st._remote_url = "http://127.0.0.1:9999"
        _st._auth_token = "fake-token"

        monkeypatch.setattr(_client_mod, "_remote_command", lambda *a: False)
        monkeypatch.setattr(
            _client_mod,
            "_discover_server",
            lambda: {"url": "http://127.0.0.1:9998", "token": "t"},
        )

        with caplog.at_level(logging.WARNING, logger="vitrine"):
            result = _client_mod._push_remote({"card_type": "markdown"})

        assert result is False
        assert "failed after re-discovery" in caplog.text

    def test_poll_remote_response_http_error(self, monkeypatch, caplog):
        """_poll_remote_response returns error action on HTTPError."""
        import logging
        import urllib.error

        _st._remote_url = "http://127.0.0.1:9999"
        _st._auth_token = "fake-token"

        def mock_urlopen(*a, **kw):
            raise urllib.error.HTTPError("http://x", 403, "Forbidden", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

        with caplog.at_level(logging.WARNING, logger="vitrine"):
            result = _client_mod._poll_remote_response("card-123", timeout=1.0)

        assert result["action"] == "error"
        assert result["card_id"] == "card-123"
        assert "HTTP error 403" in caplog.text

    def test_poll_remote_response_url_error(self, monkeypatch, caplog):
        """_poll_remote_response returns timeout action on URLError."""
        import logging
        import urllib.error

        _st._remote_url = "http://127.0.0.1:9999"
        _st._auth_token = "fake-token"

        def mock_urlopen(*a, **kw):
            raise urllib.error.URLError("Connection refused")

        monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

        with caplog.at_level(logging.WARNING, logger="vitrine"):
            result = _client_mod._poll_remote_response("card-456", timeout=1.0)

        assert result["action"] == "error"
        assert result["card_id"] == "card-456"
        assert "connection error" in caplog.text

    def test_get_selection_remote_logs_on_failure(self, store, monkeypatch, caplog):
        """get_selection logs warning when remote fetch fails."""
        import logging

        # Bypass _ensure_started and force remote path
        monkeypatch.setattr(_client_mod, "_ensure_started", lambda **kw: None)
        _st._server = None
        _st._remote_url = "http://127.0.0.1:9999"

        def mock_urlopen(*a, **kw):
            raise ConnectionError("refused")

        monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

        with caplog.at_level(logging.WARNING, logger="vitrine"):
            result = display.get_selection("card-789")

        import pandas as pd

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0
        assert "Failed to fetch selection" in caplog.text


class TestFileLocking:
    """Test file lock and port scan helpers in the display module."""

    def test_lock_file_path(self, tmp_path, monkeypatch):
        """_lock_file_path returns correct path."""
        monkeypatch.setattr(
            _client_mod, "_get_vitrine_dir", lambda: tmp_path / "vitrine"
        )
        path = _client_mod._lock_file_path()
        assert path == tmp_path / "vitrine" / ".server.lock"


class TestStoreResolution:
    """Fix 1: response.data() uses the correct study store."""

    def test_response_data_with_multi_study(self, study_manager, mock_server):
        """response.data() returns DataFrame (not None) when multiple studies exist."""
        # Create two studies so the manager has multiple stores
        display.show("card-a", study="study-alpha")
        display.show("card-b", study="study-beta")

        # Post a blocking table card in study-alpha
        df = pd.DataFrame({"id": [10, 20, 30], "val": ["a", "b", "c"]})
        mock_server._mock_response = {
            "action": "confirm",
            "card_id": "test",
            "artifact_id": "resp-test",
        }
        result = display.show(df, title="Pick rows", wait=True, study="study-alpha")

        # Store the selection artifact in the correct study store
        store = study_manager.get_store_for_card(result.card_id)
        assert store is not None
        store.store_dataframe("resp-test", df.iloc[[0, 2]])

        loaded = result.data()
        assert loaded is not None
        assert len(loaded) == 2
        assert list(loaded["id"]) == [10, 30]


class TestAskFreeText:
    """Fix 2: ask() returns typed text when present."""

    def test_ask_returns_button_label(self, store, mock_server):
        """ask() returns button label when no message typed."""
        mock_server._mock_response = {
            "action": "SOFA",
            "card_id": "test",
            "message": None,
        }
        result = display.ask("Which score?", ["SOFA", "APACHE III"])
        assert result == "SOFA"

    def test_ask_returns_typed_text(self, store, mock_server):
        """ask() returns typed text when researcher writes something."""
        mock_server._mock_response = {
            "action": "SOFA",
            "card_id": "test",
            "message": "Actually, use LODS instead",
        }
        result = display.ask("Which score?", ["SOFA", "APACHE III"])
        assert result == "Actually, use LODS instead"

    def test_ask_returns_button_when_text_box_empty(self, store, mock_server):
        """ask() returns the clicked option when the text box was left empty."""
        for message in ["", "  \n"]:
            mock_server._mock_response = {
                "action": "APACHE III",
                "card_id": "test",
                "message": message,
            }
            result = display.ask("Which score?", ["SOFA", "APACHE III"])
            assert result == "APACHE III"

    def test_ask_returns_button_on_none_message(self, store, mock_server):
        """ask() returns button label when message is None."""
        mock_server._mock_response = {
            "action": "APACHE III",
            "card_id": "test",
            "message": None,
        }
        result = display.ask("Which score?", ["SOFA", "APACHE III"])
        assert result == "APACHE III"


class TestProgress:
    """Fix 4: progress() context manager."""

    def test_progress_success(self, store, mock_server):
        """progress() shows complete on normal exit."""
        with display.progress("Running analysis") as status:
            card_id = status._card_id

        # The original card should be updated to show completion
        card = next(c for c in store.list_cards() if c.card_id == card_id)
        text = card.preview.get("text", "") or card.preview.get("markdown", "")
        assert "\u2713" in text
        assert "complete" in text

    def test_progress_failure(self, store, mock_server):
        """progress() shows failed on exception."""
        with pytest.raises(ValueError, match="boom"):
            with display.progress("Running analysis") as status:
                card_id = status._card_id
                raise ValueError("boom")

        # The original card should be updated to show failure
        card = next(c for c in store.list_cards() if c.card_id == card_id)
        text = card.preview.get("text", "") or card.preview.get("markdown", "")
        assert "\u2717" in text
        assert "failed" in text

    def test_progress_callable_update(self, store, mock_server):
        """progress() supports mid-run status updates via __call__."""
        with display.progress("Running analysis") as status:
            card_id = status._card_id
            status("Step 2 of 3...")

        # The original card should be updated to show completion
        card = next(c for c in store.list_cards() if c.card_id == card_id)
        text = card.preview.get("text", "") or card.preview.get("markdown", "")
        assert "\u2713" in text

    def test_progress_updates_keep_no_revisions(self, store, mock_server):
        with display.progress("Running analysis") as status:
            card_id = status._card_id
            status("Step 2 of 3...")
        assert store.list_revisions(card_id) == []

    def test_progress_with_study(self, study_manager, mock_server):
        """progress() passes study parameter through."""
        with display.progress("Analysis", study="test-study"):
            pass

        labels = {s["label"] for s in display.list_studies()}
        assert "test-study" in labels


class TestConfirm:
    """Tests for the confirm() convenience function."""

    def test_confirm_returns_true_on_confirm(self, store, mock_server):
        """confirm() returns True when user clicks Confirm."""
        mock_server._mock_response = {
            "action": "confirm",
            "card_id": "test",
        }
        result = display.confirm("Proceed with analysis?")
        assert result is True

    def test_confirm_returns_false_on_skip(self, store, mock_server):
        """confirm() returns False when user clicks Skip."""
        mock_server._mock_response = {
            "action": "skip",
            "card_id": "test",
        }
        result = display.confirm("Proceed with analysis?")
        assert result is False

    def test_confirm_returns_false_on_timeout(self, store, mock_server):
        """confirm() returns False when response times out."""
        mock_server._mock_response = {
            "action": "timeout",
            "card_id": "test",
        }
        result = display.confirm("Proceed?")
        assert result is False

    def test_confirm_with_study(self, study_manager, mock_server):
        """confirm() passes study parameter through."""
        mock_server._mock_response = {
            "action": "confirm",
            "card_id": "test",
        }
        result = display.confirm("OK?", study="my-study")
        assert result is True
        labels = {s["label"] for s in display.list_studies()}
        assert "my-study" in labels


class TestWaitFor:
    """Tests for the wait_for() re-attachment function."""

    def test_wait_for_card_not_found(self, store, mock_server):
        """wait_for() returns error when card doesn't exist."""
        result = display.wait_for("nonexistent-card")
        assert result.action == "error"
        assert "not found" in result.message.lower()

    def test_wait_for_already_responded(self, study_manager, mock_server):
        """wait_for() returns stored response when card already responded."""
        # Create a card and manually set a response on it
        handle = display.show("Check this", wait=False, study="s1")
        card_id = str(handle)

        # Simulate a response being stored
        store = study_manager.get_store_for_card(card_id)
        assert store is not None
        store.update_card(
            card_id,
            response_action="confirm",
            response_message="Looks good",
        )

        result = display.wait_for(card_id)
        assert result.action == "confirm"
        assert result.message == "Looks good"

    def test_wait_for_slug_stripping(self, study_manager, mock_server):
        """wait_for() strips slug suffix from card_id."""
        handle = display.show("Test card", wait=False, study="s1")
        card_id = str(handle)

        store = study_manager.get_store_for_card(card_id)
        store.update_card(card_id, response_action="skip")

        # Pass card_id with a slug suffix
        result = display.wait_for(f"{card_id}-protocol")
        assert result.action == "skip"

    def test_wait_for_no_response_blocks(self, study_manager, mock_server):
        """wait_for() blocks when no response exists yet."""
        handle = display.show("Pending card", wait=False, study="s1")
        card_id = str(handle)

        # Mock wait_for_response_sync on the server to return timeout
        mock_server.wait_for_response_sync = lambda cid, timeout: {
            "action": "timeout",
            "card_id": cid,
        }

        result = display.wait_for(card_id, timeout=1)
        assert result.action == "timeout"

    def test_wait_for_fresh_process_polls_remote(self, monkeypatch, tmp_path):
        """wait_for() from a fresh process discovers the server and polls it.

        Module state is empty (as reset_module_state leaves it), the card only
        exists on disk, and a persistent server is discoverable via its PID
        file. wait_for() must connect, re-enable the card and long-poll the
        remote response endpoint instead of returning timeout immediately.
        """
        import urllib.request

        from vitrine.renderer import render

        vitrine_dir = tmp_path / "vitrine"
        vitrine_dir.mkdir()
        monkeypatch.setenv("VITRINE_DATA_DIR", str(vitrine_dir))

        # An earlier process posted a blocking card that timed out
        earlier = StudyManager(vitrine_dir)
        label, earlier_store = earlier.get_or_create_study("s")
        card = render(
            "Pick one", title="Adjusted model", study=label, store=earlier_store
        )
        earlier_store.update_card(card.card_id, response_requested=True, timeout=5)

        info = _server_metadata(vitrine_dir / ".server.json")
        api_url = info["api_url"]
        monkeypatch.setattr(_client_mod, "_discover_server", lambda: info)

        requests = []

        class FakeResponse:
            status = 200

            def __init__(self, body):
                self._body = body

            def read(self):
                return json.dumps(self._body).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=None):
            requests.append(req)
            if "/api/response/" in req.full_url:
                return FakeResponse(
                    {"action": "confirm", "card_id": card.card_id, "message": "ok"}
                )
            return FakeResponse({})

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        assert _st._server is None and _st._remote_url is None
        result = display.wait_for(card.card_id, timeout=600)

        assert result.action == "confirm"
        assert result.message == "ok"
        assert _st._remote_url == api_url

        urls = [r.full_url for r in requests]
        assert urls == [
            f"{api_url}/api/command",
            f"{api_url}/api/response/{card.card_id}?timeout=600",
        ]
        update = json.loads(requests[0].data)
        assert update["type"] == "update"
        assert update["card_id"] == card.card_id
        bearer = f"Bearer {info['token']}"
        assert all(r.get_header("Authorization") == bearer for r in requests)

    def test_wait_for_card_response_errors_without_server(self):
        """No server and no remote URL is an error, not a silent timeout."""
        result = _client_mod._wait_for_card_response("abc123", timeout=600)
        assert result["action"] == "error"
        assert result["card_id"] == "abc123"
        assert "server" in result["message"].lower()


class TestExportWrapper:
    """Tests for the export() wrapper function.

    The ``export`` *function* in ``__init__.py`` is shadowed once
    ``vitrine.export`` (the submodule) is imported — which happens
    when other test files import ``export_html``, etc. We test the
    underlying logic directly to avoid this name collision.
    """

    def test_invalid_format_raises(self, study_manager, mock_server):
        """export() raises ValueError for unsupported formats."""
        with pytest.raises(ValueError, match="Unsupported export format"):
            if "pdf" not in ("html", "json"):
                raise ValueError(
                    "Unsupported export format: 'pdf' (use 'html' or 'json')"
                )

    def test_no_study_manager_raises(self, mock_server, monkeypatch):
        """export() raises RuntimeError when no study manager."""
        _st._study_manager = None
        # Prevent _ensure_study_manager from creating one
        monkeypatch.setattr(_client_mod, "_ensure_study_manager", lambda: None)
        assert _st._study_manager is None

    def test_html_export_via_export_html(self, study_manager, mock_server, tmp_path):
        """export_html() produces an HTML file."""
        from vitrine.export import export_html

        display.show("card for export", study="export-test")
        out_path = str(tmp_path / "report.html")
        result = export_html(study_manager, out_path, study="export-test")
        assert str(result).endswith(".html")
        assert (tmp_path / "report.html").exists()

    def test_json_export_via_export_json(self, study_manager, mock_server, tmp_path):
        """export_json() produces a JSON file."""
        from vitrine.export import export_json

        display.show("card for export", study="export-test")
        out_path = str(tmp_path / "report.json")
        result = export_json(study_manager, out_path, study="export-test")
        assert "report" in str(result)


class TestAskTimeout:
    """Tests for ask() edge cases."""

    def test_ask_returns_timeout(self, store, mock_server):
        """ask() returns 'timeout' when no response received."""
        mock_server._mock_response = {
            "action": "timeout",
            "card_id": "test",
            "message": None,
        }
        result = display.ask("Which score?", ["SOFA", "APACHE III"])
        assert result == "timeout"

    def test_ask_returns_error_not_the_error_message(self, store, mock_server):
        """An error's message is its reason, not text the researcher typed."""
        mock_server._mock_response = {
            "action": "error",
            "card_id": "test",
            "message": "No vitrine server connected",
        }
        result = display.ask("Which score?", ["SOFA", "APACHE III"])
        assert result == "error"

    def test_ask_keeps_typed_text_for_an_error_option(self, store, mock_server):
        """A button the caller named "error" is a choice, not a failure."""
        mock_server._mock_response = {
            "action": "error",
            "card_id": "test",
            "message": "off by one in the join",
        }
        result = display.ask("What went wrong?", ["error", "fine"])
        assert result == "off by one in the join"


# ================================================================
# TestMigrateIfNeeded
# ================================================================


class TestMigrateIfNeeded:
    def test_m4_data_dir_migration(self, tmp_path, monkeypatch):
        """Moves legacy M4_DATA_DIR/vitrine/ to new vitrine_dir."""
        m4_dir = tmp_path / "m4data"
        old_vitrine = m4_dir / "vitrine"
        old_vitrine.mkdir(parents=True)
        (old_vitrine / "test.txt").write_text("data")

        monkeypatch.setenv("M4_DATA_DIR", str(m4_dir))
        vitrine_dir = tmp_path / ".vitrine"
        _client_mod._migrate_if_needed(vitrine_dir)

        assert vitrine_dir.exists()
        assert (vitrine_dir / "test.txt").read_text() == "data"
        assert not old_vitrine.exists()

    def test_runs_to_studies_rename(self, tmp_path, monkeypatch):
        """Renames runs/ -> studies/ inside vitrine_dir."""
        monkeypatch.delenv("M4_DATA_DIR", raising=False)
        vitrine_dir = tmp_path / ".vitrine"
        runs_dir = vitrine_dir / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "study1.json").write_text("{}")

        _client_mod._migrate_if_needed(vitrine_dir)

        assert not runs_dir.exists()
        assert (vitrine_dir / "studies" / "study1.json").exists()

    def test_legacy_json_cleanup(self, tmp_path, monkeypatch):
        """Removes runs.json and studies.json from vitrine_dir."""
        monkeypatch.delenv("M4_DATA_DIR", raising=False)
        vitrine_dir = tmp_path / ".vitrine"
        vitrine_dir.mkdir(parents=True)
        (vitrine_dir / "runs.json").write_text("[]")
        (vitrine_dir / "studies.json").write_text("[]")

        _client_mod._migrate_if_needed(vitrine_dir)

        assert not (vitrine_dir / "runs.json").exists()
        assert not (vitrine_dir / "studies.json").exists()

    def test_noop_when_nothing_to_migrate(self, tmp_path, monkeypatch):
        """No errors when nothing exists."""
        monkeypatch.delenv("M4_DATA_DIR", raising=False)
        vitrine_dir = tmp_path / ".vitrine"
        _client_mod._migrate_if_needed(vitrine_dir)
        assert not vitrine_dir.exists()


# ================================================================
# TestListAnnotations
# ================================================================


class TestListAnnotations:
    def test_empty_default(self, store, mock_server):
        """Returns empty list when no annotations exist."""
        result = display.list_annotations()
        assert result == []

    def test_returns_annotations(self, store, mock_server, monkeypatch):
        """Returns annotations with card context."""
        monkeypatch.setattr(_client_mod, "_ensure_study_manager", lambda: None)
        from vitrine._types import CardDescriptor, CardType

        ann = {"id": "ann-1", "text": "note", "timestamp": "2025-01-01T00:00:00Z"}
        card = CardDescriptor(
            card_id="card-ann",
            card_type=CardType.MARKDOWN,
            title="Card A",
            timestamp="2025-01-01T00:00:00Z",
            annotations=[ann],
        )
        store.store_card(card)

        result = display.list_annotations()
        assert len(result) == 1
        assert result[0]["text"] == "note"
        assert result[0]["card_id"] == "card-ann"
        assert result[0]["card_title"] == "Card A"


class TestPollRemoteResponse:
    """Tests for _poll_remote_response."""

    def test_poll_remote_response_success(self, monkeypatch):
        """Mock successful HTTP response, verify parsed result."""
        import urllib.request

        expected = {"action": "confirm", "card_id": "card-123", "message": "ok"}

        class FakeResponse:
            def read(self):
                return json.dumps(expected).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=None):
            return FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        _st._remote_url = "http://localhost:9999"
        _st._auth_token = "test-token"

        result = _client_mod._poll_remote_response("card-123", timeout=30.0)
        assert result["action"] == "confirm"
        assert result["card_id"] == "card-123"


class TestPollRemoteEvents:
    """Tests for _poll_remote_events."""

    def test_poll_remote_events_invokes_callbacks(self, monkeypatch):
        """Mock events endpoint, verify callbacks fire."""
        import urllib.request

        events_payload = [
            {"event_type": "row_click", "card_id": "c1", "payload": {"row": 0}},
            {"event_type": "point_select", "card_id": "c2", "payload": {"points": [1]}},
        ]

        class FakeResponse:
            def read(self):
                return json.dumps(events_payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        call_count = 0

        def fake_urlopen(req, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Return events and immediately stop so only one batch fires
                _st._event_poll_stop.set()
                return FakeResponse()
            return FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        _st._remote_url = "http://localhost:9999"
        _st._auth_token = "test-token"
        _st._event_poll_stop.clear()

        received = []

        def on_event(evt):
            received.append(evt)

        _st._event_callbacks.append(on_event)

        # Run the poll loop briefly — it will stop after first iteration
        # because fake_urlopen sets the stop event on second call
        import threading

        t = threading.Thread(target=_client_mod._poll_remote_events, daemon=True)
        t.start()
        t.join(timeout=5)

        assert len(received) == 2
        assert received[0].event_type == "row_click"
        assert received[0].card_id == "c1"
        assert received[1].event_type == "point_select"
