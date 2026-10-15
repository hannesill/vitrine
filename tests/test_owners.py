"""Tests for owner processes: a server that stops once its owners have exited.

Tests cover:
- parse_owner_pid: values of --owner-pid and VITRINE_OWNER_PID
- process_start_time / is_same_process_alive: telling a reused PID apart
- load_server_metadata: the owners field
- DisplayServer.add_owner / owners_gone, and the PID file they rewrite
- POST /api/owner
- the server's command line and its start with owners that have exited
- a real server process stopping once its last owner exits
"""

import json
import os
import subprocess
import sys
import time

import pytest
from starlette.testclient import TestClient

import vitrine._utils as utils
import vitrine.server as server_mod
from vitrine._utils import (
    OWNER_CHECK_INTERVAL_SECONDS,
    OwnerRejected,
    ServerMetadataError,
    is_same_process_alive,
    load_server_metadata,
    owner_pids,
    parse_owner_pid,
    process_start_time,
)
from vitrine.client import _add_server_owner
from vitrine.server import DisplayServer
from vitrine.study_manager import StudyManager

_TOKEN = "owner-test-token"
_START_TIME_PLATFORM = sys.platform.startswith("linux") or sys.platform == "darwin"


def _exited_pid() -> int:
    """The PID of a process that has exited and been reaped."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def _metadata(pid_path, **overrides):
    info = {
        "pid": os.getpid(),
        "port": 7741,
        "host": "127.0.0.1",
        "url": "http://127.0.0.1:7741",
        "api_url": "http://127.0.0.1:7741",
        "session_id": "session",
        "token": "token",
        "data_dir": str(pid_path.parent.resolve()),
        "version": "1.0.0",
    }
    info.update(overrides)
    return info


class _Processes:
    """Stands in for the operating system: which PIDs run, and since when."""

    def __init__(self, monkeypatch, **start_times):
        self.start_times = {int(pid[1:]): t for pid, t in start_times.items()}
        monkeypatch.setattr(server_mod, "process_start_time", self.start_time)
        monkeypatch.setattr(server_mod, "is_same_process_alive", self.same)

    def alive(self, pid):
        return pid in self.start_times

    def start_time(self, pid):
        return self.start_times.get(pid)

    def same(self, pid, start_time):
        return self.alive(pid) and self.start_times[pid] == start_time

    def exit(self, pid):
        del self.start_times[pid]


@pytest.fixture
def server(tmp_path):
    return DisplayServer(
        study_manager=StudyManager(tmp_path / ".vitrine"),
        port=7797,
        token=_TOKEN,
        session_id="owner-test",
    )


# ---------------------------------------------------------------------------
# Reading an owner PID
# ---------------------------------------------------------------------------


class TestParseOwnerPid:
    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_means_no_owner(self, value):
        assert parse_owner_pid(value) is None

    @pytest.mark.parametrize(
        ("value", "pid"), [("123", 123), (" 42\n", 42), ("2147483647", 2**31 - 1)]
    )
    def test_positive_integers(self, value, pid):
        assert parse_owner_pid(value) == pid

    @pytest.mark.parametrize(
        "value", ["0", "-1", "+3", "1.5", "abc", "12a", "١٢", "2147483648"]
    )
    def test_anything_else_is_an_error(self, value):
        with pytest.raises(ValueError, match="positive integer"):
            parse_owner_pid(value)


# ---------------------------------------------------------------------------
# Process identity
# ---------------------------------------------------------------------------


def _seconds_since_start(start_time: int) -> float:
    """How long ago a process started, from its ``process_start_time``."""
    if sys.platform == "darwin":  # microseconds since the epoch
        return time.time() - start_time / 1_000_000
    # Linux: clock ticks after boot
    uptime = float(open("/proc/uptime").read().split()[0])
    return uptime - start_time / os.sysconf("SC_CLK_TCK")


class _FakeKernel32:
    """kernel32 for one process: open fails with ``open_error``, else its
    handle is signalled once it has ``exited``, and it was created at
    FILETIME (1, 2)."""

    def __init__(self, open_error=None, exited=False):
        self.open_error = open_error
        self.exited = exited
        self.calls = []

    def OpenProcess(self, access, inherit, pid):
        self.calls.append(("open", access, pid))
        return None if self.open_error else 7

    def WaitForSingleObject(self, handle, milliseconds):
        self.calls.append(("wait", handle, milliseconds))
        return 0 if self.exited else 0x102  # WAIT_OBJECT_0, WAIT_TIMEOUT

    def GetProcessTimes(self, handle, created, exited, kernel, user):
        created._obj.dwHighDateTime = 1
        created._obj.dwLowDateTime = 2
        return 1

    def CloseHandle(self, handle):
        self.calls.append(("close", handle))
        return 1


@pytest.fixture
def windows(monkeypatch):
    """Pretend to be on Windows; returns a function that installs a kernel32."""
    import ctypes

    monkeypatch.setattr(utils.sys, "platform", "win32")

    def install(kernel32):
        monkeypatch.setattr(utils, "_windows_kernel32", lambda: kernel32)
        monkeypatch.setattr(
            ctypes, "get_last_error", lambda: kernel32.open_error, raising=False
        )
        return kernel32

    return install


class TestProcessStartTime:
    @pytest.mark.skipif(not _START_TIME_PLATFORM, reason="Linux and macOS")
    def test_tells_processes_apart(self):
        own = process_start_time(os.getpid())
        assert isinstance(own, int)
        assert process_start_time(os.getpid()) == own

        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            child_start = process_start_time(child.pid)
            # A wrong offset or field would not land on just now.
            assert -1 < _seconds_since_start(child_start) < 10
            assert child_start >= own
            assert is_same_process_alive(child.pid, child_start)
            # The same PID with another start time is a different process.
            assert not is_same_process_alive(child.pid, child_start + 1)
        finally:
            child.kill()
            child.wait()

    @pytest.mark.skipif(not _START_TIME_PLATFORM, reason="Linux and macOS")
    def test_an_exited_process_that_is_not_reaped_is_not_alive(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        child_start = process_start_time(child.pid)
        child.kill()
        try:
            # Until its parent waits for it, its PID still answers signals.
            _wait_for(lambda: not is_same_process_alive(child.pid, child_start), 5)
            assert utils.is_pid_alive(child.pid)
            assert not is_same_process_alive(child.pid, None)
        finally:
            child.wait()

    @pytest.mark.skipif(not _START_TIME_PLATFORM, reason="Linux and macOS")
    def test_none_for_a_process_that_is_gone(self):
        assert process_start_time(_exited_pid()) is None

    def test_none_where_the_platform_does_not_say(self, monkeypatch):
        monkeypatch.setattr(utils.sys, "platform", "sunos5")
        assert process_start_time(os.getpid()) is None

    def test_pid_alone_without_a_start_time(self):
        assert is_same_process_alive(os.getpid(), None)
        assert not is_same_process_alive(_exited_pid(), None)

    def test_pid_alone_when_the_start_time_cannot_be_read(self, monkeypatch):
        monkeypatch.setattr(utils, "_process_record", lambda pid: None)
        assert is_same_process_alive(os.getpid(), 12345)

    @pytest.mark.parametrize(("state", "exited"), [("S", False), ("Z", True)])
    def test_linux_counts_fields_after_the_last_parenthesis(
        self, tmp_path, state, exited
    ):
        # The command name may hold spaces and parentheses itself.
        fields = [state] + [str(n) for n in range(4, 22)] + ["987654", "1000"]
        (tmp_path / "77").mkdir()
        (tmp_path / "77" / "stat").write_text("77 (a) (b c) " + " ".join(fields))
        assert utils._linux_process(77, proc=tmp_path) == (987654, exited)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX PIDs")
    def test_a_pid_too_large_for_the_platform_is_not_alive(self):
        assert utils.is_pid_alive(2**40) is False

    def test_windows_reads_the_creation_time(self, windows):
        kernel32 = windows(_FakeKernel32())
        assert process_start_time(4242) == (1 << 32) | 2
        assert kernel32.calls == [("open", 0x1000, 4242), ("close", 7)]

    def test_windows_none_when_the_process_cannot_be_opened(self, windows):
        windows(_FakeKernel32(open_error=87))
        assert process_start_time(4242) is None

    def test_windows_a_running_process_is_alive(self, windows):
        kernel32 = windows(_FakeKernel32())
        assert utils.is_pid_alive(4242) is True
        # SYNCHRONIZE, and a wait that does not block.
        assert kernel32.calls == [
            ("open", 0x00100000, 4242),
            ("wait", 7, 0),
            ("close", 7),
        ]

    def test_windows_an_exited_process_still_held_open_is_not_alive(self, windows):
        # Its parent's handle keeps it openable after it has exited, and its
        # exit code may be 259, which also means STILL_ACTIVE.
        kernel32 = windows(_FakeKernel32(exited=True))
        assert utils.is_pid_alive(4242) is False
        assert ("close", 7) in kernel32.calls

    def test_windows_a_process_that_may_not_be_opened_is_alive(self, windows):
        windows(_FakeKernel32(open_error=5))  # ERROR_ACCESS_DENIED
        assert utils.is_pid_alive(4242) is True

    def test_windows_a_pid_not_in_use_is_not_alive(self, windows):
        windows(_FakeKernel32(open_error=87))  # ERROR_INVALID_PARAMETER
        assert utils.is_pid_alive(4242) is False


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


class TestOwnersMetadata:
    def test_metadata_without_owners_has_none(self, tmp_path):
        pid_path = tmp_path / ".server.json"
        pid_path.write_text(json.dumps(_metadata(pid_path)))
        assert owner_pids(load_server_metadata(pid_path)) == []

    def test_owners_are_read(self, tmp_path):
        pid_path = tmp_path / ".server.json"
        owners = [{"pid": 10, "start_time": 5}, {"pid": 11, "start_time": None}]
        pid_path.write_text(json.dumps(_metadata(pid_path, owners=owners)))
        assert owner_pids(load_server_metadata(pid_path)) == [10, 11]

    @pytest.mark.parametrize(
        "owners",
        [
            {"pid": 10},
            [10],
            [{"pid": "10"}],
            [{"pid": 0}],
            [{"pid": True}],
            [{"pid": 10, "start_time": "yesterday"}],
        ],
    )
    def test_malformed_owners_are_rejected(self, tmp_path, owners):
        pid_path = tmp_path / ".server.json"
        pid_path.write_text(json.dumps(_metadata(pid_path, owners=owners)))
        with pytest.raises(ServerMetadataError, match="owners"):
            load_server_metadata(pid_path)


# ---------------------------------------------------------------------------
# DisplayServer owners
# ---------------------------------------------------------------------------


class TestDisplayServerOwners:
    def test_a_server_without_owners_never_goes(self, server):
        assert server.owner_pids == []
        assert server.owners_gone() is False

    def test_add_owner_records_its_start_time_in_the_pid_file(
        self, server, monkeypatch, tmp_path
    ):
        _Processes(monkeypatch, p10=1000)
        pid_path = tmp_path / ".vitrine" / ".server.json"
        server._write_pid_file(pid_path)
        started_at = json.loads(pid_path.read_text())["started_at"]
        assert json.loads(pid_path.read_text())["owners"] == []

        assert server.add_owner(10) == [10]

        data = json.loads(pid_path.read_text())
        assert data["owners"] == [{"pid": 10, "start_time": 1000}]
        assert data["started_at"] == started_at
        assert owner_pids(load_server_metadata(pid_path)) == [10]

    def test_an_owner_that_is_not_running_is_rejected(self, server, monkeypatch):
        _Processes(monkeypatch)
        with pytest.raises(OwnerRejected) as rejected:
            server.add_owner(10)
        assert rejected.value.reason == "not_running"
        assert server.owner_pids == []

    @pytest.mark.skipif(not _START_TIME_PLATFORM, reason="Linux and macOS")
    def test_an_owner_that_has_exited_but_is_not_reaped_is_rejected(self, server):
        # Until its parent waits for it, its PID still answers signals.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        child_start = process_start_time(child.pid)
        child.kill()
        try:
            _wait_for(lambda: not is_same_process_alive(child.pid, child_start), 5)
            with pytest.raises(OwnerRejected) as rejected:
                server.add_owner(child.pid)
            assert rejected.value.reason == "not_running"
            assert server.owner_pids == []
        finally:
            child.wait()

    def test_stops_with_the_last_owner(self, server, monkeypatch, tmp_path):
        processes = _Processes(monkeypatch, p10=1000, p11=1100)
        pid_path = tmp_path / ".vitrine" / ".server.json"
        server._write_pid_file(pid_path)
        server.add_owner(10)
        server.add_owner(11)
        assert server.owners_gone() is False

        processes.exit(10)
        assert server.owners_gone() is False
        assert server.owner_pids == [11]
        assert owner_pids(json.loads(pid_path.read_text())) == [11]

        processes.exit(11)
        assert server.owners_gone() is True
        assert server.owners_gone() is True

    def test_no_owner_is_taken_once_the_owners_are_gone(self, server, monkeypatch):
        processes = _Processes(monkeypatch, p10=1000, p12=1200)
        server.add_owner(10)
        processes.exit(10)
        assert server.owners_gone() is True

        with pytest.raises(OwnerRejected) as rejected:
            server.add_owner(12)
        assert rejected.value.reason == "stopping"

    def test_a_reused_pid_is_not_the_owner(self, server, monkeypatch):
        processes = _Processes(monkeypatch, p10=1000)
        server.add_owner(10)
        processes.start_times[10] = 2000  # owner exited; a new process got its PID
        assert server.owners_gone() is True

    def test_a_pid_given_again_takes_its_new_start_time(self, server, monkeypatch):
        processes = _Processes(monkeypatch, p10=1000)
        server.add_owner(10)
        processes.start_times[10] = 2000
        assert server.add_owner(10) == [10]
        assert server._owners == [{"pid": 10, "start_time": 2000}]
        assert server.owners_gone() is False

    def test_a_new_owner_replaces_owners_that_already_exited(self, server, monkeypatch):
        # A host restarted before the server noticed its old PID had gone.
        processes = _Processes(monkeypatch, p10=1000, p20=2000)
        server.add_owner(10)
        processes.exit(10)
        assert server.add_owner(20) == [20]
        assert server.owners_gone() is False

    def test_a_pid_file_of_another_server_is_left_alone(
        self, server, monkeypatch, tmp_path
    ):
        _Processes(monkeypatch, p10=1000)
        pid_path = tmp_path / ".vitrine" / ".server.json"
        server._write_pid_file(pid_path)
        other = json.loads(pid_path.read_text()) | {"pid": os.getpid() + 1}
        pid_path.write_text(json.dumps(other))

        server.add_owner(10)

        assert json.loads(pid_path.read_text()) == other


# ---------------------------------------------------------------------------
# POST /api/owner
# ---------------------------------------------------------------------------


class TestOwnerEndpoint:
    @pytest.fixture
    def client(self, server):
        return TestClient(server._app)

    def _post(self, client, body, token=_TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return client.post("/api/owner", json=body, headers=headers)

    def test_requires_auth(self, client):
        assert self._post(client, {"pid": 10}, token=None).status_code == 401
        assert self._post(client, {"pid": 10}, token="wrong").status_code == 401

    @pytest.mark.parametrize(
        "body",
        [{}, {"pid": "10"}, {"pid": 0}, {"pid": True}, {"pid": 2**31}, []],
    )
    def test_rejects_anything_but_a_pid(self, client, body):
        assert self._post(client, body).status_code == 400

    def test_adds_an_owner(self, client, server, monkeypatch):
        _Processes(monkeypatch, p10=1000, p11=1100)
        assert self._post(client, {"pid": 10}).json() == {"owner_pids": [10]}
        response = self._post(client, {"pid": 11})
        assert response.status_code == 200
        assert response.json() == {"owner_pids": [10, 11]}

    def test_an_owner_that_is_not_running_is_a_conflict(self, client, monkeypatch):
        _Processes(monkeypatch)
        response = self._post(client, {"pid": 10})
        assert response.status_code == 409
        assert response.json()["error"] == "not_running"

    def test_a_server_whose_owners_are_gone_is_unavailable(
        self, client, server, monkeypatch
    ):
        processes = _Processes(monkeypatch, p10=1000, p11=1100)
        server.add_owner(10)
        processes.exit(10)
        server.owners_gone()

        response = self._post(client, {"pid": 11})
        assert response.status_code == 503
        assert response.json()["error"] == "stopping"


_SERVER_INFO = {"api_url": "http://127.0.0.1:7741", "token": "secret"}


class TestAddServerOwner:
    """How the client reads the server's answer to POST /api/owner."""

    def _answer(self, monkeypatch, status=200, body=b""):
        import io
        import urllib.error
        import urllib.request

        requests = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(request, timeout):
            requests.append(request)
            if status == 200:
                return Response(body)
            raise urllib.error.HTTPError(
                request.full_url, status, "error", {}, io.BytesIO(body)
            )

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        return requests

    def test_returns_the_owners(self, monkeypatch):
        requests = self._answer(monkeypatch, body=b'{"owner_pids": [10, 11]}')
        assert _add_server_owner(_SERVER_INFO, 11) == [10, 11]
        (request,) = requests
        assert request.full_url == "http://127.0.0.1:7741/api/owner"
        assert request.get_header("Authorization") == "Bearer secret"
        assert json.loads(request.data) == {"pid": 11}

    def test_a_server_from_before_owners_takes_none(self, monkeypatch):
        self._answer(monkeypatch, status=404, body=b"Not Found")
        assert _add_server_owner(_SERVER_INFO, 11) is None

    def test_no_answer_takes_none(self, monkeypatch):
        import urllib.error
        import urllib.request

        def refuse(request, timeout):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", refuse)
        assert _add_server_owner(_SERVER_INFO, 11) is None

    @pytest.mark.parametrize(
        ("status", "reason"), [(409, "not_running"), (503, "stopping")]
    )
    def test_rejections(self, monkeypatch, status, reason):
        body = json.dumps({"error": reason, "message": "said the server"}).encode()
        self._answer(monkeypatch, status=status, body=body)
        with pytest.raises(OwnerRejected, match="said the server") as rejected:
            _add_server_owner(_SERVER_INFO, 11)
        assert rejected.value.reason == reason

    def test_a_rejection_without_a_json_body(self, monkeypatch):
        self._answer(monkeypatch, status=503, body=b"<html>")
        with pytest.raises(OwnerRejected) as rejected:
            _add_server_owner(_SERVER_INFO, 11)
        assert rejected.value.reason == "stopping"


# ---------------------------------------------------------------------------
# The server's command line
# ---------------------------------------------------------------------------


class TestServerCommandLine:
    def test_owner_pids_from_flags(self, monkeypatch):
        monkeypatch.setenv("VITRINE_OWNER_PID", "99")
        args = server_mod._parse_args(["--owner-pid", "5", "--owner-pid", "6"])
        assert args.owner_pid == [5, 6]

    def test_owner_pid_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("VITRINE_OWNER_PID", "7")
        assert server_mod._parse_args([]).owner_pid == [7]

    @pytest.mark.parametrize("env", [None, ""])
    def test_no_owner(self, monkeypatch, env):
        if env is not None:
            monkeypatch.setenv("VITRINE_OWNER_PID", env)
        assert server_mod._parse_args([]).owner_pid == []

    def test_invalid_owner_in_the_environment_is_an_error(self, monkeypatch):
        monkeypatch.setenv("VITRINE_OWNER_PID", "self")
        with pytest.raises(SystemExit) as exited:
            server_mod._parse_args([])
        assert exited.value.code == 2

    def test_invalid_owner_flag_is_an_error(self):
        with pytest.raises(SystemExit) as exited:
            server_mod._parse_args(["--owner-pid", "0"])
        assert exited.value.code == 2

    def test_does_not_start_when_no_owner_is_running(self, monkeypatch, tmp_path):
        data_dir = tmp_path / ".vitrine"
        monkeypatch.setenv("VITRINE_DATA_DIR", str(data_dir))
        _Processes(monkeypatch)
        started = []
        monkeypatch.setattr(
            DisplayServer, "start", lambda self, **kwargs: started.append(kwargs)
        )

        with pytest.raises(SystemExit) as exited:
            server_mod._run_standalone(no_open=True, owner_pids=[10])

        assert exited.value.code == 1
        assert started == []
        assert not (data_dir / ".server.json").exists()


# ---------------------------------------------------------------------------
# A real server process
# ---------------------------------------------------------------------------


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def _wait_for(condition, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"condition not met within {timeout}s")


class TestOwnedServerProcess:
    def test_stops_once_its_last_owner_exits(self, tmp_path):
        data_dir = tmp_path / ".vitrine"
        (data_dir / "studies" / "kept").mkdir(parents=True)
        pid_path = data_dir / ".server.json"
        env = {
            **os.environ,
            "VITRINE_DATA_DIR": str(data_dir),
            "VITRINE_OPEN_BROWSER": "0",
        }
        first, second = _sleeper(), _sleeper()
        command = [sys.executable, "-m", "vitrine.server", "--no-open"]
        server = subprocess.Popen(
            [*command, "--owner-pid", str(first.pid)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            info = _wait_for(
                lambda: pid_path.exists() and load_server_metadata(pid_path), 20
            )
            assert owner_pids(info) == [first.pid]

            # A second host takes the running server over as well.
            assert _add_server_owner(info, second.pid) == [first.pid, second.pid]

            first.kill()
            first.wait()
            _wait_for(
                lambda: owner_pids(load_server_metadata(pid_path)) == [second.pid],
                4 * OWNER_CHECK_INTERVAL_SECONDS,
            )
            assert server.poll() is None

            second.kill()
            second.wait()
            assert server.wait(timeout=4 * OWNER_CHECK_INTERVAL_SECONDS + 5) == 0
            assert not pid_path.exists()
            assert (data_dir / "studies" / "kept").is_dir()
        finally:
            for process in (first, second, server):
                if process.poll() is None:
                    process.kill()
                    process.wait()
