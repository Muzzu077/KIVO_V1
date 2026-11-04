"""Local IPC transport, protocol, event delivery and lifecycle tests."""
import io
import json
import os
import socket
import stat
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from kivo.app import KivoApp
from kivo.ipc.protocol import (
    EVENT_NAMES,
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
)
from kivo.ipc.server import (
    AlreadyRunningError,
    IPCError,
    IPCServer,
    default_socket_path,
)


class StaticHandlers:
    def __init__(self):
        self.cancelled = False

    def get_status(self):
        return {
            "service": "kivo",
            "version": "test",
            "protocol_version": PROTOCOL_VERSION,
            "assistant_state": "idle",
            "audio_state": "unavailable",
            "agent_state": "idle",
            "active_task": False,
            "drive": {"active": False, "goal": "", "action": "", "reason": "",
                      "elapsed": 0.0},
        }

    def get_current_task(self):
        return {"task": None}

    def get_dashboard_data(self):
        return {"status": self.get_status(), "current_task": None, "task_history": []}

    def cancel_active_task(self):
        self.cancelled = True
        return {"cancelled": False, "message": "No active tasks to cancel."}


class IPCClient:
    def __init__(self, path: Path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(3.0)
        self.sock.connect(str(path))
        self.file = self.sock.makefile("rwb")
        self.pending_events = []

    def close(self):
        try:
            self.file.close()
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def send_raw(self, raw: bytes):
        self.file.write(raw)
        self.file.flush()

    def send_request(self, operation, params=None, request_id="1",
                     version=PROTOCOL_VERSION, message_type="request"):
        payload = {
            "version": version,
            "type": message_type,
            "id": request_id,
            "operation": operation,
        }
        if params is not None:
            payload["params"] = params
        self.send_raw((json.dumps(payload) + "\n").encode())

    def read(self):
        line = self.file.readline()
        if not line:
            raise EOFError("server closed the connection")
        return json.loads(line.decode("utf-8"))

    def request(self, operation, params=None, request_id="1",
                version=PROTOCOL_VERSION):
        self.send_request(operation, params, request_id, version=version)
        while True:
            message = self.read()
            if message.get("type") == "response":
                # Protocol errors that occur before an id is parsed use null.
                if message.get("id") in (request_id, None):
                    return message
            elif message.get("type") == "event":
                self.pending_events.append(message)

    def next_event(self, event=None):
        if self.pending_events:
            for index, message in enumerate(self.pending_events):
                if event is None or message.get("event") == event:
                    return self.pending_events.pop(index)
        while True:
            message = self.read()
            if message.get("type") != "event":
                continue
            if event is None or message.get("event") == event:
                return message
            self.pending_events.append(message)


class IPCServerProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "kivo.sock"
        self.handlers = StaticHandlers()
        self.server = IPCServer(self.path, handlers=self.handlers,
                                logger=lambda _message: None)
        self.server.start()
        self.client = IPCClient(self.path)

    def tearDown(self):
        self.client.close()
        self.server.stop()
        self.tmp.cleanup()

    def test_socket_created_with_private_permissions(self):
        self.assertTrue(self.path.exists())
        info = self.path.lstat()
        self.assertTrue(stat.S_ISSOCK(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(info.st_uid, os.getuid())
        parent = self.path.parent.stat()
        self.assertEqual(stat.S_IMODE(parent.st_mode), 0o700)

    def test_status_request(self):
        message = self.client.request("get_status")
        self.assertTrue(message["ok"])
        self.assertEqual(message["result"]["service"], "kivo")
        self.assertEqual(message["result"]["protocol_version"], PROTOCOL_VERSION)

    def test_current_task_request(self):
        message = self.client.request("get_current_task")
        self.assertTrue(message["ok"])
        self.assertEqual(message["result"], {"task": None})

    def test_dashboard_request(self):
        message = self.client.request("get_dashboard_data")
        self.assertTrue(message["ok"])
        self.assertIn("status", message["result"])
        self.assertIn("task_history", message["result"])

    def test_invalid_json_keeps_connection_usable(self):
        self.client.send_raw(b"this is not json\n")
        error = self.client.read()
        self.assertFalse(error["ok"])
        self.assertEqual(error["error"]["code"], "invalid_json")
        self.assertIsNone(error["id"])

        response = self.client.request("get_status", request_id="after-error")
        self.assertTrue(response["ok"])

    def test_invalid_utf8_is_rejected(self):
        self.client.send_raw(b"\xff\xfe\n")
        error = self.client.read()
        self.assertEqual(error["error"]["code"], "invalid_json")

    def test_unsupported_operation(self):
        self.client.send_request("run_shell")
        error = self.client.read()
        self.assertFalse(error["ok"])
        self.assertEqual(error["error"]["code"], "unsupported_operation")
        self.assertEqual(error["id"], "1")

    def test_missing_request_id(self):
        self.client.send_raw(json.dumps({
            "version": PROTOCOL_VERSION,
            "type": "request",
            "operation": "get_status",
        }).encode() + b"\n")
        error = self.client.read()
        self.assertEqual(error["error"]["code"], "invalid_request")

    def test_wrong_protocol_version(self):
        message = self.client.request("get_status", version=99)
        self.assertFalse(message["ok"])
        self.assertEqual(message["error"]["code"], "unsupported_version")

    def test_non_integer_protocol_versions_are_rejected(self):
        for value in (True, "1", 1.0, None):
            with self.subTest(version=value):
                message = self.client.request("get_status", version=value)
                self.assertFalse(message["ok"])
                self.assertEqual(message["error"]["code"], "unsupported_version")

    def test_wrong_message_type(self):
        self.client.send_request("get_status", message_type="event")
        error = self.client.read()
        self.assertEqual(error["error"]["code"], "invalid_type")

    def test_params_rejected_for_no_param_operation(self):
        message = self.client.request("get_status", params={"secret": "x"})
        self.assertFalse(message["ok"])
        self.assertEqual(message["error"]["code"], "invalid_params")
        self.assertEqual(message["id"], "1")

    def test_invalid_event_filter(self):
        message = self.client.request(
            "subscribe_events", params={"events": ["not_an_event"]})
        self.assertFalse(message["ok"])
        self.assertEqual(message["error"]["code"], "invalid_params")
        self.assertEqual(message["id"], "1")

    def test_oversized_message_is_rejected_and_closed(self):
        self.client.send_raw(b"x" * (MAX_MESSAGE_BYTES + 100) + b"\n")
        error = self.client.read()
        self.assertFalse(error["ok"])
        self.assertEqual(error["error"]["code"], "message_too_large")
        with self.assertRaises(EOFError):
            self.client.read()

    def test_subscribe_receive_and_unsubscribe(self):
        message = self.client.request(
            "subscribe_events", params={"events": ["drive_progress"]})
        self.assertTrue(message["ok"])
        self.assertEqual(message["result"]["events"], ["drive_progress"])

        self.server.publish("drive_progress", {"goal": "g", "action": "a"})
        event = self.client.next_event()
        self.assertEqual(event["event"], "drive_progress")
        self.assertIn("sequence", event)

        response = self.client.request("unsubscribe_events")
        self.assertTrue(response["ok"])
        self.assertFalse(response["result"]["subscribed"])

        self.server.publish("drive_progress", {"goal": "g", "action": "b"})
        # No event should be queued; a request gets the next frame.
        status = self.client.request("get_status", request_id="after-unsub")
        self.assertEqual(status["type"], "response")

    def test_unsubscribed_client_does_not_receive_events(self):
        self.server.publish("assistant_state_changed", {"state": "thinking"})
        status = self.client.request("get_status", request_id="plain")
        self.assertEqual(status["type"], "response")

    def test_multiple_clients_receive_subscribed_events(self):
        second = IPCClient(self.path)
        try:
            self.client.request("subscribe_events", params={"events": ["drive_started"]})
            second.request("subscribe_events", params={"events": ["drive_started"]})
            self.server.publish("drive_started", {"goal": "both"})
            first_event = self.client.next_event()
            second_event = second.next_event()
            self.assertEqual(first_event["event"], "drive_started")
            self.assertEqual(second_event["event"], "drive_started")
            self.assertEqual(first_event["data"]["goal"], "both")
        finally:
            second.close()

    def test_client_disconnection_does_not_stop_server(self):
        self.client.close()
        replacement = IPCClient(self.path)
        try:
            response = replacement.request("get_status")
            self.assertTrue(response["ok"])
        finally:
            replacement.close()
        # Prevent tearDown from closing an already-closed helper twice.
        self.client = IPCClient(self.path)

    def test_duplicate_server_startup_is_refused(self):
        other = IPCServer(self.path, handlers=StaticHandlers(),
                          logger=lambda _message: None)
        with self.assertRaises(AlreadyRunningError):
            other.start()
        self.assertTrue(self.client.request("get_status")["ok"])


class IPCServerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "kivo.sock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_stale_socket_is_recovered(self):
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.path))
        stale.close()
        self.assertTrue(self.path.exists())

        server = IPCServer(self.path, handlers=StaticHandlers(),
                           logger=lambda _message: None)
        server.start()
        try:
            client = IPCClient(self.path)
            try:
                self.assertTrue(client.request("get_status")["ok"])
            finally:
                client.close()
        finally:
            server.stop()

    def test_non_socket_file_is_never_replaced(self):
        self.path.write_text("important user file", encoding="utf-8")
        server = IPCServer(self.path, handlers=StaticHandlers(),
                           logger=lambda _message: None)
        with self.assertRaises(IPCError):
            server.start()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "important user file")

    def test_public_directory_is_refused(self):
        open_dir = Path(self.tmp.name) / "public"
        open_dir.mkdir(mode=0o755)
        os.chmod(open_dir, 0o755)
        server = IPCServer(open_dir / "kivo.sock", handlers=StaticHandlers(),
                           logger=lambda _message: None)
        with self.assertRaises(IPCError):
            server.start()

    def test_shutdown_removes_socket_and_lock(self):
        server = IPCServer(self.path, handlers=StaticHandlers(),
                           logger=lambda _message: None)
        server.start()
        client = IPCClient(self.path)
        client.close()

        server.stop()

        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.with_name(self.path.name + ".lock").exists())
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            with self.assertRaises(OSError):
                probe.connect(str(self.path))
        finally:
            probe.close()

    def test_shutdown_is_idempotent(self):
        server = IPCServer(self.path, handlers=StaticHandlers(),
                           logger=lambda _message: None)
        server.start()
        server.stop()
        server.stop()

    def test_explicit_missing_parent_is_not_created(self):
        missing = Path(self.tmp.name) / "missing" / "kivo.sock"
        server = IPCServer(missing, handlers=StaticHandlers(),
                           logger=lambda _message: None)
        with self.assertRaises(IPCError):
            server.start()
        self.assertFalse(missing.parent.exists())


class DefaultSocketPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _with_runtime(self, value):
        return mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(value)})

    def test_default_path_uses_private_runtime_directory(self):
        with self._with_runtime(self.root):
            path = default_socket_path()
        self.assertEqual(path, self.root / "kivo" / "kivo.sock")

    def test_missing_runtime_directory_is_refused(self):
        with self._with_runtime(self.root / "does-not-exist"):
            with self.assertRaises(IPCError):
                default_socket_path()

    def test_empty_runtime_directory_is_refused(self):
        with self._with_runtime(""):
            with self.assertRaises(IPCError):
                default_socket_path()

    def test_relative_runtime_directory_is_refused(self):
        with self._with_runtime("relative/runtime"):
            with self.assertRaises(IPCError):
                default_socket_path()

    def test_public_runtime_directory_is_refused(self):
        public = self.root / "public"
        public.mkdir(mode=0o755)
        os.chmod(public, 0o755)
        with self._with_runtime(public):
            with self.assertRaises(IPCError):
                default_socket_path()

    def test_runtime_directory_that_is_a_file_is_refused(self):
        not_a_dir = self.root / "file"
        not_a_dir.write_text("not a directory", encoding="utf-8")
        with self._with_runtime(not_a_dir):
            with self.assertRaises(IPCError):
                default_socket_path()


class KivoAppIPCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "kivo.sock"
        self.app = KivoApp(socket_path=self.path)
        with redirect_stdout(io.StringIO()):
            self.assertTrue(self.app.initialize())
        self.client = IPCClient(self.path)

    def tearDown(self):
        try:
            self.client.close()
        except Exception:
            pass
        with redirect_stdout(io.StringIO()):
            self.app.shutdown()

        from core.drive import get_drive_session
        from core.screen_context import get_screen_context
        from core.task_orchestrator import get_task_orchestrator

        get_task_orchestrator()._active_tasks.clear()
        get_task_orchestrator()._task_history.clear()
        get_drive_session().stop("test cleanup")
        get_screen_context().clear()
        self.tmp.cleanup()

    def test_status_reflects_real_backend_state(self):
        result = self.client.request("get_status")["result"]
        self.assertEqual(result["assistant_state"], "idle")
        self.assertEqual(result["audio_state"], "unavailable")
        self.assertEqual(result["agent_state"], "idle")
        self.assertFalse(result["active_task"])
        self.assertFalse(result["drive"]["active"])
        self.assertEqual(result["protocol_version"], PROTOCOL_VERSION)

    def test_dashboard_exposes_no_secrets_or_memory(self):
        result = self.client.request("get_dashboard_data")["result"]
        serialized = json.dumps(result).lower()
        for forbidden in ("api_key", "password", "voiceprint", "memory",
                          "gemini", "secret"):
            self.assertNotIn(forbidden, serialized)

    def test_task_history_does_not_leak_tool_error_text(self):
        from core.task_orchestrator import StepStatus, get_task_orchestrator

        orchestrator = get_task_orchestrator()
        plan = orchestrator.create_plan("Failure privacy check", [
            {"tool": "noop", "description": "first step"},
        ])
        plan.steps[0].status = StepStatus.FAILED
        plan.is_finished = True
        plan.success = False
        plan.summary = (
            "Task failed at step 1 (first step): SECRET_TOOL_OUTPUT"
        )
        orchestrator._finalize_plan(plan)

        history = self.client.request("get_dashboard_data")["result"]["task_history"]
        serialized = json.dumps(history)
        self.assertNotIn("SECRET_TOOL_OUTPUT", serialized)
        self.assertIn("Task failed at step 1.", serialized)
        self.assertTrue(any(item["task_id"] == plan.task_id for item in history))

    def test_current_task_and_cancel(self):
        from core.task_orchestrator import get_task_orchestrator

        orchestrator = get_task_orchestrator()
        plan = orchestrator.create_plan("IPC task", [
            {"tool": "noop", "description": "first step"},
        ])

        result = self.client.request("get_current_task")["result"]
        self.assertIsNotNone(result["task"])
        self.assertEqual(result["task"]["task_id"], plan.task_id)
        self.assertEqual(result["task"]["goal"], "IPC task")

        cancelled = self.client.request("cancel_active_task")["result"]
        self.assertTrue(cancelled["cancelled"])
        self.assertIn("IPC task", cancelled["message"])

        after = self.client.request("get_current_task")["result"]
        self.assertIsNone(after["task"])

    def test_drive_events_are_published(self):
        from core.drive import get_drive_session

        self.client.request("subscribe_events", params={"events": [
            "drive_started", "drive_progress", "drive_completed",
        ]})
        drive = get_drive_session()
        drive.start("IPC drive goal")
        started = self.client.next_event("drive_started")
        self.assertEqual(started["data"]["goal"], "IPC drive goal")

        drive.update("step one")
        progress = self.client.next_event("drive_progress")
        self.assertEqual(progress["data"]["action"], "step one")

        drive.stop("finished")
        completed = self.client.next_event("drive_completed")
        self.assertEqual(completed["data"]["reason"], "finished")

    def test_task_events_and_agent_state(self):
        from core.task_orchestrator import get_task_orchestrator

        self.client.request("subscribe_events", params={"events": [
            "task_history_updated", "agent_state_changed",
        ]})
        get_task_orchestrator().create_plan("Event task", [
            {"tool": "noop", "description": "step one"},
        ])

        events = [self.client.next_event(), self.client.next_event()]
        names = {event["event"] for event in events}
        self.assertIn("task_history_updated", names)
        self.assertIn("agent_state_changed", names)
        task_event = next(e for e in events if e["event"] == "task_history_updated")
        self.assertEqual(task_event["data"]["reason"], "created")
        self.assertTrue(task_event["data"]["active_task"])
        self.assertEqual(task_event["data"]["current_task"]["goal"], "Event task")

        agent_event = next(e for e in events if e["event"] == "agent_state_changed")
        self.assertEqual(agent_event["data"]["state"], "working")

    def test_screen_context_event_excludes_ocr_and_image(self):
        from core.screen_context import get_screen_context

        class Observation:
            timestamp = time.monotonic()
            active_app = "Alacritty"
            active_title = "shell"
            ocr_text_preview = "private text that must not leave the backend"
            is_terminal = True
            is_browser = False
            terminal_cwd = "/tmp"
            frame_jpeg = b"fake-jpeg"
            active_window_id = 1

        self.client.request("subscribe_events", params={
            "events": ["screen_context_updated"]})
        get_screen_context().remember(Observation())
        event = self.client.next_event("screen_context_updated")
        self.assertEqual(event["data"]["active_app"], "Alacritty")
        self.assertNotIn("ocr", json.dumps(event["data"]).lower())
        self.assertNotIn("private text", json.dumps(event["data"]))
        self.assertNotIn("fake-jpeg", json.dumps(event["data"]))

    def test_multiple_frontend_clients(self):
        from core.drive import get_drive_session

        second = IPCClient(self.path)
        try:
            self.client.request("subscribe_events", params={"events": ["drive_started"]})
            second.request("subscribe_events", params={"events": ["drive_started"]})
            get_drive_session().start("multi-client")
            first = self.client.next_event("drive_started")
            other = second.next_event("drive_started")
            self.assertEqual(first["data"]["goal"], "multi-client")
            self.assertEqual(other["data"]["goal"], "multi-client")
        finally:
            second.close()

    def test_duplicate_app_startup_is_refused(self):
        other = KivoApp(socket_path=self.path)
        with redirect_stdout(io.StringIO()):
            self.assertFalse(other.initialize())
            other.shutdown()
        # The original server is still healthy.
        self.assertTrue(self.client.request("get_status")["ok"])

    def test_graceful_shutdown_removes_socket(self):
        with redirect_stdout(io.StringIO()):
            self.app.shutdown()
        self.assertFalse(self.path.exists())

    def test_assistant_state_changes_are_events(self):
        self.client.request("subscribe_events", params={
            "events": ["assistant_state_changed"]})
        self.app._runtime_state.set_assistant_state("thinking", "IPC test")
        event = self.client.next_event("assistant_state_changed")
        self.assertEqual(event["data"]["state"], "thinking")
        self.assertEqual(event["data"]["previous"], "idle")


if __name__ == "__main__":
    unittest.main()
