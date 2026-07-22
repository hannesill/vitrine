"""Tests for vitrine.cli — standalone vitrine CLI commands.

Tests cover:
- status command: running and no-server states
- stop command: successful stop and no-server-found
- start command: already running, background start
- restart command: stop + start flow
"""

import json
from unittest.mock import patch

from typer.testing import CliRunner

from vitrine.cli import app

runner = CliRunner()

# The CLI functions use lazy imports like:
#   from vitrine import server_status, stop_server
# So we patch on the vitrine module itself.


def _running_info() -> dict:
    return {
        "pid": 12345,
        "port": 7741,
        "host": "127.0.0.1",
        "url": "http://vitrine.localhost:7741",
        "api_url": "http://127.0.0.1:7741",
        "session_id": "sess-123",
        "started_at": "2024-01-01T00:00:00",
        "token": "must-not-leak",
    }


def _parse_single_json_line(output: str) -> dict:
    assert len(output.splitlines()) == 1
    return json.loads(output)


class TestStatusCommand:
    def test_status_no_server(self):
        with patch("vitrine.server_status", return_value=None):
            result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "No running server" in result.output

    def test_status_server_running(self):
        info = {
            "pid": 12345,
            "port": 7741,
            "url": "http://127.0.0.1:7741",
            "session_id": "sess-123",
            "started_at": "2024-01-01T00:00:00",
        }
        with patch("vitrine.server_status", return_value=info):
            result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "running" in result.output.lower()
        assert "12345" in result.output
        assert "7741" in result.output

    def test_human_no_server_output_is_unchanged(self):
        with patch("vitrine.server_status", return_value=None):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert result.output == "> No running server found.\n"

    def test_status_json_running_has_stable_safe_shape(
        self, monkeypatch, tmp_path
    ):
        data_dir = tmp_path / "project" / ".vitrine"
        monkeypatch.setenv("VITRINE_DATA_DIR", str(data_dir))

        with patch("vitrine.server_status", return_value=_running_info()):
            result = runner.invoke(app, ["status", "--json"])

        assert result.exit_code == 0
        payload = _parse_single_json_line(result.output)
        assert payload == {
            "status": "running",
            "url": "http://vitrine.localhost:7741",
            "api_url": "http://127.0.0.1:7741",
            "pid": 12345,
            "port": 7741,
            "session_id": "sess-123",
            "data_dir": str(data_dir.resolve()),
            "version": "0.1.0",
            "error": None,
        }
        assert "token" not in payload

    def test_status_json_not_running_uses_exit_three(self, monkeypatch, tmp_path):
        data_dir = tmp_path / ".vitrine"
        monkeypatch.setenv("VITRINE_DATA_DIR", str(data_dir))

        with patch("vitrine.server_status", return_value=None):
            result = runner.invoke(app, ["status", "--json"])

        assert result.exit_code == 3
        payload = _parse_single_json_line(result.output)
        assert payload["status"] == "stopped"
        assert payload["data_dir"] == str(data_dir.resolve())
        assert payload["version"] == "0.1.0"
        assert payload["pid"] is None
        assert payload["error"] is None

    def test_status_json_tracks_explicit_data_directory(
        self, monkeypatch, tmp_path
    ):
        with patch("vitrine.server_status", return_value=None):
            directories = []
            for project in ("a", "b"):
                data_dir = tmp_path / project / ".vitrine"
                monkeypatch.setenv("VITRINE_DATA_DIR", str(data_dir))
                result = runner.invoke(app, ["status", "--json"])
                directories.append(_parse_single_json_line(result.output)["data_dir"])

        assert directories == [
            str((tmp_path / "a" / ".vitrine").resolve()),
            str((tmp_path / "b" / ".vitrine").resolve()),
        ]


class TestStopCommand:
    def test_stop_no_server(self):
        with patch("vitrine.stop_server", return_value=False):
            result = runner.invoke(app, ["stop"])
        assert result.exit_code == 0
        assert "No running server" in result.output

    def test_stop_success(self):
        with patch("vitrine.stop_server", return_value=True):
            result = runner.invoke(app, ["stop"])
        assert result.exit_code == 0
        assert "stopped" in result.output.lower()

    def test_human_no_server_output_is_unchanged(self):
        with patch("vitrine.stop_server", return_value=False):
            result = runner.invoke(app, ["stop"])

        assert result.exit_code == 0
        assert result.output == "> No running server found.\n"

    def test_stop_json_success(self, monkeypatch, tmp_path):
        data_dir = tmp_path / ".vitrine"
        monkeypatch.setenv("VITRINE_DATA_DIR", str(data_dir))
        with (
            patch("vitrine.server_status", return_value=_running_info()),
            patch("vitrine.stop_server", return_value=True),
        ):
            result = runner.invoke(app, ["stop", "--json"])

        assert result.exit_code == 0
        payload = _parse_single_json_line(result.output)
        assert payload["status"] == "stopped"
        assert payload["data_dir"] == str(data_dir.resolve())
        assert payload["pid"] is None

    def test_stop_json_failure(self):
        with (
            patch("vitrine.server_status", return_value=_running_info()),
            patch("vitrine.stop_server", return_value=False),
        ):
            result = runner.invoke(app, ["stop", "--json"])

        assert result.exit_code == 1
        payload = _parse_single_json_line(result.output)
        assert payload["status"] == "failed"
        assert payload["error"] == "Failed to stop the running server."


class TestStartCommand:
    def test_start_already_running(self):
        info = {"pid": 12345, "port": 7741, "url": "http://127.0.0.1:7741"}
        with patch("vitrine.server_status", return_value=info):
            result = runner.invoke(app, ["start"])
        assert result.exit_code == 0
        assert "already running" in result.output.lower()

    def test_start_json_reuses_running_server(self):
        with (
            patch("vitrine.server_status", return_value=_running_info()),
            patch("vitrine.cli._start_background") as start_background,
        ):
            result = runner.invoke(app, ["start", "--no-open", "--json"])

        assert result.exit_code == 0
        payload = _parse_single_json_line(result.output)
        assert payload["status"] == "running"
        assert payload["session_id"] == "sess-123"
        start_background.assert_not_called()

    def test_start_background_success(self):
        # First call: no server running; subsequent calls: server is up
        call_count = 0

        def mock_server_status():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None  # Not running initially
            return {"pid": 12345, "port": 7741, "url": "http://127.0.0.1:7741"}

        with (
            patch("vitrine.server_status", side_effect=mock_server_status),
            patch("subprocess.Popen") as mock_popen,
            patch("vitrine.cli.time.sleep"),
        ):
            result = runner.invoke(app, ["start", "--port", "7741"])

        assert result.exit_code == 0
        assert "started" in result.output.lower()
        mock_popen.assert_called_once()

    def test_start_background_json_success(self):
        call_count = 0

        def mock_server_status():
            nonlocal call_count
            call_count += 1
            return None if call_count == 1 else _running_info()

        with (
            patch("vitrine.server_status", side_effect=mock_server_status),
            patch("subprocess.Popen") as mock_popen,
            patch("vitrine.cli.time.sleep"),
        ):
            result = runner.invoke(app, ["start", "--no-open", "--json"])

        assert result.exit_code == 0
        payload = _parse_single_json_line(result.output)
        assert payload["status"] == "running"
        assert payload["url"] == "http://vitrine.localhost:7741"
        assert payload["api_url"] == "http://127.0.0.1:7741"
        assert "token" not in payload
        assert "--no-open" in mock_popen.call_args.args[0]

    def test_start_background_timeout(self):
        """Server doesn't come up within deadline — exit code 1."""

        # Make time.monotonic advance past the 5s deadline
        clock = [0.0]

        def mock_monotonic():
            clock[0] += 3.0  # Jump 3s each call
            return clock[0]

        with (
            patch("vitrine.server_status", return_value=None),
            patch("subprocess.Popen"),
            patch("vitrine.cli.time.monotonic", side_effect=mock_monotonic),
            patch("vitrine.cli.time.sleep"),
        ):
            result = runner.invoke(app, ["start"])

        assert result.exit_code == 1
        assert "didn't become healthy" in result.output.lower()

    def test_start_json_reports_early_process_exit(self):
        process = type("Process", (), {"poll": lambda self: 7})()
        with (
            patch("vitrine.server_status", return_value=None),
            patch("subprocess.Popen", return_value=process),
        ):
            result = runner.invoke(app, ["start", "--no-open", "--json"])

        assert result.exit_code == 1
        payload = _parse_single_json_line(result.output)
        assert payload["status"] == "failed"
        assert "exit code 7" in payload["error"]

    def test_start_json_reports_spawn_failure(self):
        with (
            patch("vitrine.server_status", return_value=None),
            patch("subprocess.Popen", side_effect=OSError("not executable")),
        ):
            result = runner.invoke(app, ["start", "--no-open", "--json"])

        assert result.exit_code == 1
        payload = _parse_single_json_line(result.output)
        assert payload["status"] == "failed"
        assert "not executable" in payload["error"]

    def test_start_json_rejects_foreground(self):
        with patch("vitrine.server_status", return_value=None):
            result = runner.invoke(app, ["start", "--foreground", "--json"])

        assert result.exit_code == 1
        payload = _parse_single_json_line(result.output)
        assert payload["status"] == "failed"
        assert "--foreground" in payload["error"]


class TestRestartCommand:
    def test_restart_no_existing_server(self):
        call_count = 0

        def mock_server_status():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return None  # No server at initial check
            return {"pid": 99, "port": 7741, "url": "http://127.0.0.1:7741"}

        with (
            patch("vitrine.server_status", side_effect=mock_server_status),
            patch("subprocess.Popen"),
            patch("vitrine.cli.time.sleep"),
        ):
            result = runner.invoke(app, ["restart"])

        assert result.exit_code == 0
        assert "starting fresh" in result.output.lower()

    def test_restart_stops_existing_server(self):
        call_count = 0

        def mock_server_status():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"pid": 111, "port": 7741, "url": "http://127.0.0.1:7741"}
            if call_count == 2:
                return None  # After stop, not started yet
            return {"pid": 222, "port": 7741, "url": "http://127.0.0.1:7741"}

        with (
            patch("vitrine.server_status", side_effect=mock_server_status),
            patch("vitrine.stop_server", return_value=True) as mock_stop,
            patch("subprocess.Popen"),
            patch("vitrine.cli.time.sleep"),
        ):
            result = runner.invoke(app, ["restart"])

        assert result.exit_code == 0
        mock_stop.assert_called_once()
        assert "stopped" in result.output.lower()

    def test_restart_stop_failure(self):
        """If stop fails, restart exits with error."""
        with (
            patch(
                "vitrine.server_status",
                return_value={"pid": 111, "port": 7741},
            ),
            patch("vitrine.stop_server", return_value=False),
        ):
            result = runner.invoke(app, ["restart"])

        assert result.exit_code == 1
        assert "failed to stop" in result.output.lower()
