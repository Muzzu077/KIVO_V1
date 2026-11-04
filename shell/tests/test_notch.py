"""Stage 3 verification for the KIVO Dynamic Notch.

The tests exercise the real Quickshell process in the running niri session.
They use a private copy of ``shell/`` and a fake protocol-v1 Unix socket, so
they never touch the live KIVO socket or the user's running shell instance.

Run from the repository root:

    python -m unittest discover -s shell/tests -v

``Qt 6 qmllint`` is required for the static test (the Qt 5 binary with the
same name is not compatible).  The integration tests are skipped
automatically when no Wayland/niri session is available.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SHELL_DIR = REPO_ROOT / "shell"
QS = shutil.which("qs") or shutil.which("quickshell")
NIRI = shutil.which("niri")


def _find_qt6_qmllint() -> str | None:
    """Return a Qt 6 qmllint, not the unrelated Qt 5 binary in /usr/bin.

    Some distributions install Qt 5's ``qmllint`` (version 1.0) earlier in
    ``PATH`` than Qt 6's.  Qt 5's linter cannot parse QML 6 and exits 255
    without a diagnostic, so verify the version explicitly.
    """
    candidates = [
        shutil.which("qmllint6"),
        "/usr/lib/qt6/bin/qmllint",
        shutil.which("qmllint"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            result = subprocess.run(
                [candidate, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if re.match(r"qmllint\s+6\.", result.stdout.strip()):
            return candidate
    return None


QMLLINT = _find_qt6_qmllint()

EVENT_NAMES = [
    "assistant_state_changed",
    "audio_state_changed",
    "user_transcript",
    "assistant_response",
    "drive_started",
    "drive_progress",
    "drive_completed",
    "drive_failed",
    "drive_cancelled",
    "task_history_updated",
    "screen_context_updated",
    "confirmation_required",
    "error",
]


def _tmp_base() -> str:
    preferred = Path("/tmp/opencode")
    if preferred.is_dir() and os.access(preferred, os.W_OK):
        return str(preferred)
    return tempfile.gettempdir()


def _wayland_ready() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY")) and bool(
        os.environ.get("XDG_RUNTIME_DIR")
    )


def _niri_layers() -> list[dict]:
    result = subprocess.run(
        [NIRI, "msg", "-j", "layers"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise AssertionError("niri msg layers failed: " + result.stderr.strip())
    return json.loads(result.stdout)


def _notch_layer(namespace: str) -> dict | None:
    for layer in _niri_layers():
        if layer.get("namespace") == namespace:
            return layer
    return None


def _dashboard_layer() -> dict | None:
    for layer in _niri_layers():
        if str(layer.get("namespace", "")).startswith("kivo-dashboard"):
            return layer
    return None


class QmlStaticTest(unittest.TestCase):
    def test_expected_files_exist(self) -> None:
        expected = [
            "shell.qml",
            "components/DynamicNotch.qml",
            "components/NotchCharacterIcon.qml",
            "components/Waveform.qml",
            "components/TaskProgress.qml",
            "components/StatusIndicator.qml",
            "components/StopButton.qml",
            "components/KivoDashboard.qml",
            "components/DashboardOverview.qml",
            "components/DashboardActivity.qml",
            "components/DashboardAgents.qml",
            "components/DashboardDesktopContext.qml",
            "components/DashboardSettings.qml",
            "services/KivoIpc.qml",
            "styles/Theme.qml",
        ]
        for relative in expected:
            self.assertTrue(
                (SHELL_DIR / relative).is_file(),
                f"missing {relative}",
            )

    @unittest.skipUnless(QMLLINT, "Qt 6 qmllint is not installed")
    def test_qmllint(self) -> None:
        files = [SHELL_DIR / "shell.qml"]
        files.extend(sorted((SHELL_DIR / "components").glob("*.qml")))
        files.extend(sorted((SHELL_DIR / "services").glob("*.qml")))
        files.extend(sorted((SHELL_DIR / "styles").glob("*.qml")))
        result = subprocess.run(
            [QMLLINT, *[str(path) for path in files]],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(
            result.returncode,
            0,
            f"qmllint ({QMLLINT}) failed with exit {result.returncode}:\n"
            + result.stdout
            + result.stderr,
        )


class FakeBackend:
    """A small protocol-v1 Unix socket server used to drive the notch."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._clients: list[socket.socket] = []
        self._server: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._running = False
        self.requests: list[dict] = []
        self.sequence = 0
        self.accept_count = 0
        self.cancel_result = {
            "cancelled": True,
            "message": "Cancelling: Organize the workspace",
        }
        self.status = {
            "service": "kivo",
            "version": "test",
            "protocol_version": 1,
            "pid": os.getpid(),
            "assistant_state": "idle",
            "assistant_detail": "backend started",
            "audio_state": "unavailable",
            "audio_detail": "audio pipeline is not initialized",
            "agent_state": "idle",
            "agent_detail": "no active task",
            "uptime_seconds": 1.0,
            "active_task": False,
            "drive": {
                "active": False,
                "goal": "",
                "action": "",
                "reason": "",
                "elapsed": 0.0,
            },
        }
        self.current_task: dict | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.path))
        self._server.listen(8)
        self._server.settimeout(0.2)
        self._running = True
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="fake-kivo-accept", daemon=True
        )
        self._accept_thread.start()

    def stop(self) -> None:
        self._running = False
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        self.drop_clients()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def restart(self) -> None:
        self.stop()
        self.start()

    # ── Server loop ────────────────────────────────────────────────────────
    def _accept_loop(self) -> None:
        while self._running:
            server = self._server
            if server is None:
                break
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(0.2)
            with self._lock:
                self._clients.append(conn)
                self.accept_count += 1
            threading.Thread(
                target=self._client_loop,
                args=(conn,),
                name="fake-kivo-client",
                daemon=True,
            ).start()

    def _client_loop(self, conn: socket.socket) -> None:
        buffer = b""
        while self._running:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                self._handle(conn, message)
        with self._lock:
            if conn in self._clients:
                self._clients.remove(conn)
        try:
            conn.close()
        except OSError:
            pass

    def _handle(self, conn: socket.socket, message: dict) -> None:
        if message.get("type") != "request":
            return
        operation = message.get("operation")
        request_id = message.get("id")
        with self._lock:
            self.requests.append(
                {
                    "operation": operation,
                    "params": message.get("params"),
                    "id": request_id,
                    "at": time.monotonic(),
                }
            )
            status = copy.deepcopy(self.status)
            task = copy.deepcopy(self.current_task)
            cancel_result = dict(self.cancel_result)

        if operation == "get_status":
            result = status
        elif operation == "get_current_task":
            result = {"task": task}
        elif operation == "subscribe_events":
            result = {"subscribed": True, "events": EVENT_NAMES}
        elif operation == "unsubscribe_events":
            result = {"subscribed": False}
        elif operation == "cancel_active_task":
            result = cancel_result
        else:
            self._send(
                conn,
                {
                    "version": 1,
                    "type": "response",
                    "id": request_id,
                    "ok": False,
                    "error": {
                        "code": "unsupported_operation",
                        "message": f"unsupported operation: {operation}",
                    },
                },
            )
            return

        self._send(
            conn,
            {
                "version": 1,
                "type": "response",
                "id": request_id,
                "ok": True,
                "result": result,
            },
        )

    def _send(self, conn: socket.socket, message: dict) -> None:
        payload = (json.dumps(message) + "\n").encode("utf-8")
        try:
            conn.sendall(payload)
        except OSError:
            pass

    # ── Driving the notch ──────────────────────────────────────────────────
    def publish(self, event: str, data: dict, sequence: int | None = None) -> int:
        with self._lock:
            if sequence is None:
                self.sequence += 1
                sequence = self.sequence
            else:
                self.sequence = max(self.sequence, sequence)
            clients = list(self._clients)
        message = {
            "version": 1,
            "type": "event",
            "event": event,
            "sequence": sequence,
            "data": data,
        }
        for conn in clients:
            self._send(conn, message)
        return sequence

    def send_raw(self, text: str) -> None:
        payload = text.encode("utf-8")
        with self._lock:
            clients = list(self._clients)
        for conn in clients:
            try:
                conn.sendall(payload)
            except OSError:
                pass

    def drop_clients(self) -> None:
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for conn in clients:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    def wait_for_request(self, operation: str, count: int = 1,
                         timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                found = sum(
                    1 for request in self.requests
                    if request["operation"] == operation
                )
            if found >= count:
                return
            time.sleep(0.05)
        raise AssertionError(
            f"timed out waiting for {count} {operation} request(s); "
            f"seen {self.request_summary()}"
        )

    def request_summary(self) -> list[str]:
        with self._lock:
            return [str(request["operation"]) for request in self.requests]

    def count_requests(self, operation: str) -> int:
        with self._lock:
            return sum(
                1 for request in self.requests
                if request["operation"] == operation
            )

    def set_status(self, **values) -> None:
        with self._lock:
            self.status.update(values)

    def set_task(self, task: dict | None, active: bool = True) -> None:
        with self._lock:
            self.current_task = copy.deepcopy(task)
            self.status["active_task"] = bool(active and task is not None)


class ShellProcess:
    """A private Quickshell instance pointed at a test socket."""

    def __init__(
        self,
        config_dir: Path,
        socket_path: Path,
        log_dir: Path,
        env_overrides: dict[str, str] | None = None,
        debug_wayland: bool = False,
    ):
        self.config_dir = Path(config_dir)
        self.socket_path = Path(socket_path)
        self.log_path = Path(log_dir) / f"qs-{uuid.uuid4().hex}.log"
        self.namespace = f"kivo-notch-{uuid.uuid4().hex[:8]}"
        env = os.environ.copy()
        env["KIVO_SOCKET"] = str(self.socket_path)
        env["KIVO_NOTCH_NAMESPACE"] = self.namespace
        env["KIVO_NOTCH_AUTO_COLLAPSE"] = "0"
        env["KIVO_NOTCH_FOCUS_ON_EXPAND"] = "0"
        if env_overrides:
            env.update({str(key): str(value) for key, value in env_overrides.items()})
        if debug_wayland:
            env["WAYLAND_DEBUG"] = "client"
        self._log_handle = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [QS, "-p", str(self.config_dir), "--no-color"],
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(REPO_ROOT),
        )
        try:
            self._wait_ready()
        except Exception:
            self.stop()
            raise

    # ── Process helpers ────────────────────────────────────────────────────
    def _wait_ready(self) -> None:
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(
                    "Quickshell exited during startup:\n" + self.log_text()[-4000:]
                )
            result = self._ipc("show", timeout=5)
            if result.returncode == 0 and "target kivo-notch" in result.stdout:
                return
            if "Failed to load configuration" in self.log_text():
                raise AssertionError(
                    "Quickshell failed to load the notch:\n"
                    + self.log_text()[-4000:]
                )
            time.sleep(0.2)
        raise AssertionError(
            "timed out waiting for the notch IpcHandler:\n"
            + self.log_text()[-4000:]
        )

    def _ipc(self, *args: str, timeout: float = 8.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [QS, "ipc", "-p", str(self.config_dir), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )

    def call(self, function: str, *args: str) -> str:
        result = self._ipc("call", "kivo-notch", function, *args)
        if result.returncode != 0:
            raise AssertionError(
                f"qs ipc call {function} failed:\n{result.stdout}{result.stderr}"
            )
        return result.stdout.strip()

    def snapshot(self) -> dict:
        text = self.call("snapshot")
        if not text:
            raise AssertionError("empty notch snapshot")
        return json.loads(text)

    def wait_snapshot(self, predicate, timeout: float = 12.0,
                      interval: float = 0.1) -> dict:
        deadline = time.monotonic() + timeout
        last: dict | None = None
        while time.monotonic() < deadline:
            last = self.snapshot()
            if predicate(last):
                return last
            time.sleep(interval)
        raise AssertionError(
            "snapshot condition not met; last="
            + json.dumps(last, sort_keys=True)
            + "\nlog tail:\n"
            + self.log_text()[-3000:]
        )

    def wait_state(self, state: str, timeout: float = 12.0) -> dict:
        return self.wait_snapshot(lambda snapshot: snapshot["state"] == state, timeout)

    def log_text(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        try:
            self._log_handle.close()
        except OSError:
            pass


def _parse_input_regions(log_text: str) -> list[tuple[int, tuple[int, int, int, int]]]:
    """Return (surface_id, (x, y, w, h)) for each wl_surface.set_input_region."""
    regions: list[tuple[int, tuple[int, int, int, int]]] = []
    pending: tuple[int, tuple[int, int, int, int]] | None = None
    for line in log_text.splitlines():
        add = re.search(
            r"wl_region#(\d+)\.add\((-?\d+), (-?\d+), (-?\d+), (-?\d+)\)",
            line,
        )
        if add:
            pending = (
                int(add.group(1)),
                (
                    int(add.group(2)),
                    int(add.group(3)),
                    int(add.group(4)),
                    int(add.group(5)),
                ),
            )
            continue
        set_region = re.search(
            r"wl_surface#(\d+)\.set_input_region\(wl_region#(\d+)\)",
            line,
        )
        if set_region:
            surface = int(set_region.group(1))
            region = int(set_region.group(2))
            if pending is not None and pending[0] == region:
                regions.append((surface, pending[1]))
            pending = None
    return regions


@unittest.skipUnless(QS, "Quickshell (qs) is not installed")
@unittest.skipUnless(
    NIRI and _wayland_ready(),
    "a live niri Wayland session is required for integration tests",
)
class NotchIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            subprocess.run(
                [NIRI, "msg", "version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise unittest.SkipTest(f"niri is not reachable: {exc}") from exc
        cls.tmp_root = Path(
            tempfile.mkdtemp(prefix="kivo-notch-test-", dir=_tmp_base())
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp_root, ignore_errors=True)

    def setUp(self) -> None:
        self.shells: list[ShellProcess] = []
        self.backends: list[FakeBackend] = []

    def tearDown(self) -> None:
        for shell in reversed(self.shells):
            shell.stop()
        for backend in reversed(self.backends):
            backend.stop()

    # ── Helpers ────────────────────────────────────────────────────────────
    def new_socket_path(self) -> Path:
        return self.tmp_root / f"kivo-{uuid.uuid4().hex}.sock"

    def start_backend(self) -> FakeBackend:
        backend = FakeBackend(self.new_socket_path())
        backend.start()
        self.backends.append(backend)
        return backend

    def start_shell(
        self,
        backend: FakeBackend | None = None,
        env_overrides: dict[str, str] | None = None,
        debug_wayland: bool = False,
    ) -> ShellProcess:
        socket_path = backend.path if backend is not None else self.new_socket_path()
        config_dir = self.tmp_root / f"shell-{uuid.uuid4().hex}"
        shutil.copytree(
            SHELL_DIR,
            config_dir,
            ignore=shutil.ignore_patterns("tests", "__pycache__"),
        )
        shell = ShellProcess(
            config_dir,
            socket_path,
            self.tmp_root,
            env_overrides=env_overrides,
            debug_wayland=debug_wayland,
        )
        self.shells.append(shell)
        return shell

    def wait_layer(self, namespace: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            layer = _notch_layer(namespace)
            if layer is not None:
                return layer
            last = layer
            time.sleep(0.1)
        raise AssertionError(f"layer {namespace!r} was not created; last={last!r}")

    def wait_layer_focus(self, namespace: str, expected: str,
                         timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            layer = _notch_layer(namespace)
            if layer is None:
                last = None
                time.sleep(0.1)
                continue
            last = layer.get("keyboard_interactivity")
            if last is not None and str(last).lower() == expected.lower():
                return
            time.sleep(0.1)
        raise AssertionError(
            f"expected keyboard interactivity {expected!r}, saw {last!r}"
        )

    def wait_dashboard_layer(self, timeout: float = 8.0) -> dict:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            layer = _dashboard_layer()
            if layer is not None:
                return layer
            last = layer
            time.sleep(0.1)
        raise AssertionError(f"dashboard layer did not appear; last={last!r}")

    # ── Tests ──────────────────────────────────────────────────────────────
    def test_shell_starts_with_backend_unavailable(self) -> None:
        shell = self.start_shell()
        snapshot = shell.wait_state("offline")
        self.assertFalse(snapshot["connected"])
        self.assertEqual(snapshot["connectionState"], "reconnecting")
        self.assertTrue(snapshot["stale"])
        self.assertGreaterEqual(snapshot["retryInSeconds"], 0)
        self.assertIn("Could not connect", snapshot["lastError"])
        log = shell.log_text()
        self.assertIn("Configuration Loaded", log)
        self.assertNotIn("Failed to load configuration", log)
        layer = self.wait_layer(shell.namespace)
        self.assertTrue(layer["output"])

    def test_status_sync_and_all_visual_states(self) -> None:
        backend = self.start_backend()
        shell = self.start_shell(backend)
        snapshot = shell.wait_state("idle")
        self.assertTrue(snapshot["connected"])
        self.assertEqual(snapshot["connectionState"], "connected")
        self.assertFalse(snapshot["stale"])
        backend.wait_for_request("get_status")
        backend.wait_for_request("get_current_task")
        backend.wait_for_request("subscribe_events")

        backend.publish(
            "assistant_state_changed",
            {"state": "listening", "previous": "idle", "detail": "wake word"},
        )
        shell.wait_state("listening")

        backend.publish(
            "assistant_state_changed",
            {"state": "thinking", "previous": "listening", "detail": "planning"},
        )
        shell.wait_state("thinking")

        backend.publish(
            "assistant_state_changed",
            {"state": "speaking", "previous": "thinking", "detail": "answer"},
        )
        shell.wait_state("speaking")

        backend.publish(
            "drive_started",
            {"goal": "Organize the workspace", "action": "Scanning files"},
        )
        snapshot = shell.wait_state("driving")
        self.assertTrue(snapshot["driveActive"])
        self.assertEqual(snapshot["driveGoal"], "Organize the workspace")
        self.assertEqual(snapshot["driveAction"], "Scanning files")

        backend.publish(
            "drive_progress",
            {"goal": "Organize the workspace", "action": "Grouping files"},
        )
        shell.wait_snapshot(lambda item: item["driveAction"] == "Grouping files")

        backend.publish(
            "drive_completed",
            {"goal": "Organize the workspace", "reason": "done"},
        )
        snapshot = shell.wait_state("success")
        self.assertFalse(snapshot["driveActive"])
        self.assertEqual(snapshot["transientState"], "success")

        backend.publish(
            "assistant_state_changed",
            {"state": "error", "previous": "idle", "detail": "attention"},
        )
        shell.wait_state("error")

        backend.publish(
            "assistant_state_changed",
            {"state": "cancellation", "previous": "error", "detail": "stopped"},
        )
        shell.wait_state("cancelled")

        backend.publish(
            "assistant_state_changed",
            {"state": "idle", "previous": "cancellation", "detail": "ready"},
        )
        shell.wait_state("idle")

    def test_task_progress_and_cancel_flow(self) -> None:
        backend = self.start_backend()
        task = {
            "task_id": "task_demo",
            "goal": "Organize the workspace",
            "workspace_id": "",
            "created_at": time.time() - 12,
            "current_step": 2,
            "total_steps": 4,
            "status": "running",
            "success": False,
            "summary": "",
            "steps": [
                {
                    "id": 1,
                    "description": "Scan files",
                    "status": "completed",
                    "is_critical": True,
                },
                {
                    "id": 2,
                    "description": "Group by project",
                    "status": "running",
                    "is_critical": True,
                },
                {
                    "id": 3,
                    "description": "Move stale files",
                    "status": "pending",
                    "is_critical": False,
                },
                {
                    "id": 4,
                    "description": "Summarize",
                    "status": "pending",
                    "is_critical": False,
                },
            ],
        }
        backend.set_task(task, active=True)
        backend.set_status(agent_state="working", agent_detail=task["goal"])
        shell = self.start_shell(backend)

        snapshot = shell.wait_snapshot(
            lambda item: item["activeTask"] and item["taskStatus"] == "running"
        )
        self.assertEqual(snapshot["taskGoal"], task["goal"])
        self.assertEqual(snapshot["taskCurrentStep"], 2)
        self.assertEqual(snapshot["taskTotalSteps"], 4)
        self.assertTrue(snapshot["cancellable"])
        self.assertEqual(snapshot["state"], "driving")

        self.assertEqual(shell.call("requestStop"), "true")
        backend.wait_for_request("cancel_active_task")
        snapshot = shell.wait_snapshot(lambda item: item["stopping"])
        self.assertFalse(snapshot["cancellable"])
        self.assertEqual(
            snapshot["lastCancellation"]["message"],
            "Cancelling: Organize the workspace",
        )

        # A second click must not send a duplicate cancellation.
        self.assertEqual(shell.call("requestStop"), "false")
        time.sleep(0.4)
        self.assertEqual(backend.count_requests("cancel_active_task"), 1)

        finished = dict(task)
        finished.update(
            {
                "status": "cancelled",
                "success": False,
                "summary": "Task cancelled before execution.",
                "current_step": 2,
            }
        )
        backend.publish(
            "task_history_updated",
            {
                "reason": "finished",
                "active_task": False,
                "current_task": None,
                "task_history": [finished],
            },
        )
        snapshot = shell.wait_state("cancelled")
        self.assertFalse(snapshot["stopping"])
        self.assertFalse(snapshot["activeTask"])
        self.assertEqual(snapshot["transientState"], "cancelled")
        self.assertIn("cancelled", snapshot["transientDetail"].lower())
        self.assertTrue(snapshot["lastCancellation"]["ok"])
        self.assertTrue(snapshot["lastCancellation"]["cancelled"])

    def test_invalid_stale_and_gapped_messages(self) -> None:
        backend = self.start_backend()
        shell = self.start_shell(backend)
        shell.wait_state("idle")

        backend.publish(
            "assistant_state_changed",
            {"state": "listening", "previous": "idle", "detail": "wake word"},
            sequence=10,
        )
        shell.wait_state("listening")

        backend.send_raw("{this is not json}\n")
        shell.wait_snapshot(lambda item: item["invalidMessages"] >= 1)

        # A duplicate sequence must not overwrite newer authoritative state.
        backend.publish(
            "assistant_state_changed",
            {"state": "error", "previous": "listening", "detail": "stale"},
            sequence=10,
        )
        shell.wait_snapshot(lambda item: item["staleEvents"] >= 1)
        self.assertEqual(shell.snapshot()["state"], "listening")

        backend.send_raw(
            json.dumps(
                {
                    "version": 2,
                    "type": "event",
                    "event": "assistant_state_changed",
                    "sequence": 11,
                    "data": {"state": "error"},
                }
            )
            + "\n"
        )
        shell.wait_snapshot(lambda item: item["invalidMessages"] >= 2)

        backend.send_raw(
            json.dumps(
                {
                    "version": 1,
                    "type": "event",
                    "event": "assistant_state_changed",
                    "data": {"state": "error"},
                }
            )
            + "\n"
        )
        shell.wait_snapshot(lambda item: item["invalidMessages"] >= 3)

        # A gap is legal but recorded; the event is still applied.
        backend.publish(
            "assistant_state_changed",
            {"state": "thinking", "previous": "listening", "detail": "gap"},
            sequence=15,
        )
        snapshot = shell.wait_state("thinking")
        self.assertGreaterEqual(snapshot["sequenceGaps"], 1)
        self.assertTrue(snapshot["connected"])
        self.assertTrue(snapshot["lastProtocolWarning"])

    def test_reconnect_refreshes_authoritative_state(self) -> None:
        backend = self.start_backend()
        shell = self.start_shell(backend)
        shell.wait_state("idle")
        backend.wait_for_request("get_status")
        first_status_count = backend.count_requests("get_status")

        backend.stop()
        shell.wait_snapshot(lambda item: not item["connected"], timeout=8)
        self.assertEqual(shell.snapshot()["state"], "offline")

        backend.set_status(
            assistant_state="thinking",
            assistant_detail="reconnected",
            agent_state="planning",
            agent_detail="replanning",
        )
        backend.restart()
        snapshot = shell.wait_state("thinking", timeout=25)
        self.assertTrue(snapshot["connected"])
        self.assertFalse(snapshot["stale"])
        backend.wait_for_request("get_status", count=first_status_count + 1, timeout=10)
        backend.wait_for_request("subscribe_events", count=2, timeout=10)

    def test_expansion_pinning_and_auto_collapse(self) -> None:
        backend = self.start_backend()
        shell = self.start_shell(backend)
        shell.wait_state("idle")
        # The collapsed width itself animates from the offline width to the
        # idle width, so sample it only after the transition has settled.
        time.sleep(0.8)
        collapsed = shell.snapshot()
        self.assertEqual(collapsed["state"], "idle")
        self.assertFalse(collapsed["expanded"])
        collapsed_width = collapsed["notchWidth"]

        shell.call("expand")
        # The capsule width is animated; wait for the animation to settle
        # instead of sampling a mid-flight frame.
        expanded = shell.wait_snapshot(
            lambda item: item["expanded"] and item["notchWidth"] == 520
        )
        self.assertGreater(expanded["notchWidth"], collapsed_width)

        shell.call("collapse")
        collapsed_again = shell.wait_snapshot(
            lambda item: not item["expanded"]
            and item["notchWidth"] == collapsed_width
        )
        self.assertEqual(collapsed_again["notchWidth"], collapsed_width)

        shell.call("pin", "true")
        pinned = shell.wait_snapshot(lambda item: item["pinned"])
        self.assertTrue(pinned["expanded"])
        shell.call("pin", "false")
        shell.wait_snapshot(lambda item: not item["pinned"])

        shell.call("toggle")
        shell.wait_snapshot(lambda item: not item["expanded"])

        # A real completed Drive session auto-collapses when enabled.
        shell.stop()
        auto = self.start_shell(
            backend,
            env_overrides={"KIVO_NOTCH_AUTO_COLLAPSE": "1"},
        )
        auto.wait_state("idle")
        auto.call("expand")
        auto.wait_snapshot(lambda item: item["expanded"])
        backend.publish(
            "drive_started",
            {"goal": "Auto collapse demo", "action": "Starting"},
        )
        auto.wait_state("driving")
        backend.publish(
            "drive_completed",
            {"goal": "Auto collapse demo", "reason": "done"},
        )
        auto.wait_state("success")
        auto.wait_snapshot(lambda item: not item["expanded"], timeout=8)

    def test_focus_is_none_when_collapsed(self) -> None:
        shell = self.start_shell(
            env_overrides={"KIVO_NOTCH_FOCUS_ON_EXPAND": "1"}
        )
        shell.wait_state("offline")
        self.wait_layer_focus(shell.namespace, "none")
        shell.call("expand")
        shell.wait_snapshot(lambda item: item["expanded"])
        self.wait_layer_focus(shell.namespace, "ondemand")
        shell.call("collapse")
        shell.wait_snapshot(lambda item: not item["expanded"])
        self.wait_layer_focus(shell.namespace, "none")

    def test_input_region_is_notch_only(self) -> None:
        shell = self.start_shell(debug_wayland=True)
        collapsed = shell.wait_state("offline")
        self.assertFalse(collapsed["expanded"])
        collapsed_box = (
            collapsed["notchX"],
            collapsed["notchY"],
            collapsed["notchWidth"],
            collapsed["notchHeight"],
        )

        time.sleep(0.5)
        regions = _parse_input_regions(shell.log_text())
        self.assertTrue(regions, "no wl_surface.set_input_region was observed")
        panel_surface = regions[0][0]
        panel_regions = [box for surface, box in regions if surface == panel_surface]
        self.assertIn(collapsed_box, panel_regions)
        self.assertLess(collapsed_box[2], collapsed["panelWidth"])
        self.assertLess(collapsed_box[3], collapsed["panelHeight"])

        shell.call("expand")
        expanded = shell.wait_snapshot(
            lambda item: item["expanded"] and item["notchWidth"] == 520
        )
        time.sleep(1.0)
        regions = _parse_input_regions(shell.log_text())
        panel_regions = [box for surface, box in regions if surface == panel_surface]
        expanded_box = (
            expanded["notchX"],
            expanded["notchY"],
            expanded["notchWidth"],
            expanded["notchHeight"],
        )
        self.assertEqual(panel_regions[-1], expanded_box)
        self.assertLess(expanded_box[2], expanded["panelWidth"])
        self.assertLess(expanded_box[3], expanded["panelHeight"])

    def test_dashboard_lifecycle_and_notch_independence(self) -> None:
        backend = self.start_backend()
        shell = self.start_shell(backend)
        shell.wait_state("idle")
        self.assertFalse(shell.snapshot()["dashboardOpen"])

        shell.call("openDashboard")
        opened = shell.wait_snapshot(
            lambda item: item["dashboardOpen"] and item["dashboardWindowWidth"] == 880,
            timeout=15,
        )
        self.assertEqual(opened["dashboardWindowWidth"], 880)
        self.assertEqual(opened["dashboardWindowHeight"], 620)
        layer = self.wait_dashboard_layer()
        self.assertEqual(layer.get("namespace", "").startswith("kivo-dashboard"), True)
        self.assertEqual(layer.get("layer"), "Top")

        # The notch stays independently controllable while the dashboard is open.
        shell.call("collapse")
        shell.wait_snapshot(lambda item: not item["expanded"], timeout=12)
        shell.call("closeDashboard")
        shell.wait_snapshot(lambda item: not item["dashboardOpen"], timeout=12)
        self.assertFalse(shell.snapshot()["dashboardOpen"])

    def test_dashboard_disconnected_state(self) -> None:
        shell = self.start_shell()
        shell.wait_state("offline")
        shell.call("openDashboard")
        opened = shell.wait_snapshot(
            lambda item: item["dashboardOpen"] and item["state"] == "offline",
            timeout=15,
        )
        self.assertTrue(opened["dashboardOpen"])
        self.assertEqual(opened["state"], "offline")
        shell.call("closeDashboard")
        shell.wait_snapshot(lambda item: not item["dashboardOpen"], timeout=12)

    def test_audio_capture_control_and_recording_state(self) -> None:
        backend = self.start_backend()
        shell = self.start_shell(backend)
        shell.wait_state("idle")

        snap = shell.snapshot()
        self.assertFalse(snap.get("audioCapturing", False))

        # Backend simulates PTT capture start
        backend.publish(
            "audio_state_changed",
            {"state": "capturing", "previous": "idle", "detail": "recording"},
        )
        shell.wait_snapshot(lambda item: item.get("audioCapturing") is True, timeout=8)

        # Backend simulates capture complete
        backend.publish(
            "audio_capture_completed",
            {"capture_id": "c-123", "duration": 1.5, "samplerate": 16000},
        )
        backend.publish(
            "audio_state_changed",
            {"state": "idle", "previous": "capturing", "detail": "ready"},
        )
        shell.wait_snapshot(lambda item: item.get("audioCapturing") is False, timeout=8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
