#!/usr/bin/env python3
"""KIVO application lifecycle.

Starts the existing backend modules without the legacy PyQt6 interface, then
opens the local Unix-domain IPC bridge used by a future Quickshell frontend.
No microphone, screen observer, wake-word listener or model session is opened
in this stage.
"""
from __future__ import annotations

import signal
import sys
import threading
import traceback
from pathlib import Path


class KivoApp:
    """Owns backend startup, the local IPC server and orderly shutdown."""

    def __init__(self, socket_path: str | Path | None = None):
        self._running = False
        self._initialized = False
        self._shutdown_started = False
        self._shutdown_event = threading.Event()
        self._components: dict[str, object] = {}
        self._threads: list[threading.Thread] = []
        self._socket_path = Path(socket_path) if socket_path is not None else None

        self._runtime_state = None
        self._ipc = None
        self._drive_was_active = False
        self._drive_last_action = ""

        self._drive_listener = self._on_drive_update
        self._screen_listener = self._on_screen_context_update
        self._task_listener = self._on_task_event
        self._runtime_listener = self._on_runtime_state_event

    # ── Signals ────────────────────────────────────────────────────────────

    def setup_signal_handlers(self) -> None:
        """Install SIGINT/SIGTERM handlers for Ctrl+C and service stops."""
        try:
            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)
        except ValueError:
            # Not running in the main thread (e.g. an embedding test).
            pass

    def _signal_handler(self, signum, _frame) -> None:
        if self._shutdown_started:
            return
        print(f"\nReceived signal {signum}, shutting down...")
        self.shutdown()

    # ── Startup ────────────────────────────────────────────────────────────

    def initialize(self) -> bool:
        """Initialize backend components, then start the IPC bridge."""
        if self._initialized:
            return True

        print("Initializing KIVO backend components...")

        try:
            print("  [1/10] Loading configuration...")
            self._load_config()

            print("  [2/10] Initializing memory...")
            self._init_memory()

            print("  [3/10] Initializing brain...")
            self._init_brain()

            print("  [4/10] Initializing screen context store...")
            self._init_screen_context()

            print("  [5/10] Loading actions...")
            self._init_actions()

            print("  [6/10] Initializing task orchestrator...")
            self._init_task_orchestrator()

            print("  [7/10] Initializing Drive session...")
            self._init_drive()

            print("  [8/10] Initializing confirmation service...")
            self._init_confirmation()

            print("  [9/10] Initializing audio subsystem...")
            self._init_audio()

            print("  [10/10] Starting telemetry, microphone, clipboard, shelf, bridge, and IPC server...")
            self._init_telemetry()
            self._init_microphone()
            self._init_clipboard()
            self._init_shelf()
            self._init_bridge()
            self._init_ipc()

            self._initialized = True
            print("All components initialized successfully.")
            return True

        except Exception as exc:
            print(f"Initialization failed: {exc}")
            traceback.print_exc()
            self.shutdown()
            print("Startup aborted; resources released.")
            return False

    def _load_config(self) -> None:
        """Read API keys without printing their values."""
        from memory.config_manager import is_configured, load_api_keys

        self._components["api_keys"] = load_api_keys()
        if not is_configured():
            print("  note: no API key configured; model-backed features are disabled")

    def _init_memory(self) -> None:
        from memory.memory_manager import get_layered_memory

        self._components["memory"] = get_layered_memory()

    def _init_brain(self) -> None:
        from core.brain import get_brain

        self._components["brain"] = get_brain()

    def _init_screen_context(self) -> None:
        from core.screen_context import get_screen_context

        # This only creates the in-memory store; no screen capture starts.
        store = get_screen_context()
        store.add_listener(self._screen_listener)
        self._components["screen_context"] = store

    def _init_actions(self) -> None:
        from core.action_loader import discover_actions

        actions_dir = Path(__file__).resolve().parent.parent / "actions"
        self._components["action_registry"] = discover_actions(actions_dir)

    def _init_task_orchestrator(self) -> None:
        from core.task_orchestrator import get_task_orchestrator

        orchestrator = get_task_orchestrator()
        orchestrator.bind_registry(self._components["action_registry"])
        orchestrator.set_progress_callback(self._on_task_progress)
        orchestrator.add_listener(self._task_listener)
        self._components["task_orchestrator"] = orchestrator

    def _init_drive(self) -> None:
        from core.drive import get_drive_session

        drive = get_drive_session()
        drive.add_listener(self._drive_listener)
        self._components["drive"] = drive
        self._drive_was_active = False
        self._drive_last_action = ""

    def _init_confirmation(self) -> None:
        """Load the confirmation service without pretending a UI exists.

        `confirm.request()` fails closed while no show/hide callbacks are
        bound, so an irreversible action is refused instead of being parked
        behind a banner nobody can see.  Stage 2 deliberately does not expose
        confirmation resolution over IPC.
        """
        from core import confirm

        self._components["confirm_gate"] = confirm
        print("  note: no confirmation UI bound; irreversible actions are refused")

    def _init_audio(self) -> None:
        from kivo.audio import AudioCaptureManager, AudioDeviceManager, AudioIngress
        from kivo.stt import FasterWhisperEngine, TranscriptionManager, load_stt_config

        device_manager = AudioDeviceManager()
        ingress = AudioIngress()
        capture_manager = AudioCaptureManager(
            device_manager,
            on_state_change=self._on_audio_state_change,
            on_complete=self._on_audio_complete,
        )

        # Initialize STT engine and TranscriptionManager
        stt_cfg = load_stt_config()
        stt_engine = FasterWhisperEngine(
            model_name=stt_cfg.get("model", "base"),
            device=stt_cfg.get("device", "cpu"),
        )
        transcription_mgr = TranscriptionManager(
            ingress=ingress,
            engine=stt_engine,
            language=stt_cfg.get("language", "auto"),
            on_status_change=self._on_stt_status_change,
            on_transcription_started=self._on_stt_started,
            on_transcription_completed=self._on_stt_completed,
            on_transcription_failed=self._on_stt_failed,
        )
        transcription_mgr.start()

        # Initialize Conversational Brain (ConversationManager)
        from kivo.conversation import ConversationManager
        from kivo.files.tools import BoundedFileWorker

        # The application owns the read-only filesystem worker's final
        # shutdown. ConversationManager receives it so it can cancel active
        # work and pending approvals before application teardown.
        file_worker = BoundedFileWorker()
        conv_mgr = ConversationManager(
            action_registry=self._components.get("action_registry"),
            on_state_change=self._on_conversation_state_change,
            on_user_message=self._on_conversation_user_message,
            on_response_started=self._on_conversation_response_started,
            on_response_delta=self._on_conversation_response_delta,
            on_response_completed=self._on_conversation_response_completed,
            on_response_failed=self._on_conversation_response_failed,
            on_cancelled=self._on_conversation_cancelled,
            on_tool_started=self._on_agent_tool_started,
            on_tool_completed=self._on_agent_tool_completed,
            on_approval_requested=self._on_command_approval_requested,
            on_approval_resolved=self._on_command_approval_resolved,
            file_worker=file_worker,
        )

        # Initialize MPRIS MediaService
        from kivo.media import MediaService
        media_service = MediaService(
            on_status_changed=self._on_media_status_changed,
            on_lyrics_loaded=self._on_media_lyrics_loaded,
        )
        media_service.start()

        self._components["audio_device_manager"] = device_manager
        self._components["audio_capture_manager"] = capture_manager
        self._components["audio_ingress"] = ingress
        self._components["transcription_manager"] = transcription_mgr
        self._components["conversation_manager"] = conv_mgr
        self._components["file_worker"] = file_worker
        self._components["media_service"] = media_service

    def _init_telemetry(self) -> None:
        """Start read-only telemetry service."""
        try:
            from kivo.telemetry import TelemetryService

            telemetry = TelemetryService(
                on_snapshot=self._on_telemetry_snapshot,
            )
            if telemetry.start():
                self._components["telemetry_service"] = telemetry
            else:
                print("  note: telemetry service could not start")
        except Exception:
            # Telemetry is optional: a failure must not abort the backend.
            print("  note: telemetry service unavailable")

    def _init_microphone(self) -> None:
        """Start default microphone mute service."""
        try:
            from kivo.microphone import MicrophoneService

            mic = MicrophoneService(
                on_state_changed=self._on_mic_state_changed,
            )
            if mic.start():
                self._components["microphone_service"] = mic
            else:
                print("  note: microphone mute service could not start")
        except Exception:
            # Microphone service is optional: failure must not abort backend.
            print("  note: microphone service unavailable")

    def _init_clipboard(self) -> None:
        """Start clipboard intelligence service."""
        try:
            from kivo.clipboard import ClipboardService

            clipboard = ClipboardService(
                on_history_changed=self._on_clip_history_changed,
            )
            if clipboard.start():
                self._components["clipboard_service"] = clipboard
            else:
                print("  note: clipboard service could not start")
        except Exception:
            print("  note: clipboard service unavailable")

    def _init_shelf(self) -> None:
        """Start file shelf reference-staging service."""
        try:
            from kivo.shelf import FileShelfService

            shelf = FileShelfService(
                on_items_changed=self._on_shelf_items_changed,
            )
            if shelf.start():
                self._components["shelf_service"] = shelf
            else:
                print("  note: file shelf service could not start")
        except Exception:
            print("  note: file shelf service unavailable")

    def _init_bridge(self) -> None:
        """Initialize phone-to-PC bridge service."""
        try:
            from kivo.bridge import PhoneBridgeService

            bridge = PhoneBridgeService(
                on_status_changed=self._on_bridge_status_changed,
                on_pairing_requested=self._on_bridge_pairing_requested,
                on_transfer_event=self._on_bridge_transfer_event,
                shelf_service=self._components.get("shelf_service"),
            )
            self._components["bridge_service"] = bridge
        except Exception:
            print("  note: phone bridge service unavailable")

    def _init_ipc(self) -> None:
        from kivo.ipc.handlers import IPCHandlers
        from kivo.ipc.server import IPCServer
        from kivo.runtime_state import RuntimeState

        state = RuntimeState()
        state.add_listener(self._runtime_listener)

        try:
            handlers = IPCHandlers(
                state,
                self._components["drive"],
                self._components["task_orchestrator"],
                audio_device_manager=self._components.get("audio_device_manager"),
                audio_capture_manager=self._components.get("audio_capture_manager"),
                audio_ingress=self._components.get("audio_ingress"),
                transcription_manager=self._components.get("transcription_manager"),
                conversation_manager=self._components.get("conversation_manager"),
                media_service=self._components.get("media_service"),
                telemetry_service=self._components.get("telemetry_service"),
                microphone_service=self._components.get("microphone_service"),
                clipboard_service=self._components.get("clipboard_service"),
                shelf_service=self._components.get("shelf_service"),
                bridge_service=self._components.get("bridge_service"),
            )
            server = IPCServer(
                self._socket_path,
                handlers=handlers,
                logger=print,
            )
            server.start()
        except Exception:
            state.remove_listener(self._runtime_listener)
            raise

        self._runtime_state = state
        self._ipc = server
        self._components["runtime_state"] = state
        self._components["ipc"] = server
        self._publish_initial_state()

    def _publish_initial_state(self) -> None:
        if self._runtime_state is None:
            return
        snapshot = self._runtime_state.snapshot()
        self._publish("assistant_state_changed", {
            "state": snapshot["assistant_state"],
            "previous": None,
            "detail": snapshot["assistant_detail"],
        })
        self._publish("audio_state_changed", {
            "state": snapshot["audio_state"],
            "previous": None,
            "detail": snapshot["audio_detail"],
        })
        self._publish("agent_state_changed", {
            "state": snapshot["agent_state"],
            "previous": None,
            "detail": snapshot["agent_detail"],
        })
        self._publish("screen_context_updated", self._screen_snapshot(None))
        telemetry = self._components.get("telemetry_service")
        if telemetry is not None:
            self._publish("telemetry_snapshot_changed", telemetry.latest_snapshot().to_dict())
        mic = self._components.get("microphone_service")
        if mic is not None:
            self._publish("mic_state_changed", mic.get_state().to_dict())

    # ── Backend event adapters ─────────────────────────────────────────────

    def _publish(self, event: str, data: dict) -> None:
        server = self._ipc
        if server is None:
            return
        try:
            server.publish(event, data)
        except Exception as exc:  # noqa: BLE001 - event delivery is best effort
            print(f"[IPC] could not publish {event}: {exc}")

    def _on_runtime_state_event(self, event: str, data: dict) -> None:
        self._publish(event, data)

    def _on_task_progress(self, task_id: str, step: int, total: int,
                          message: str) -> None:
        print(f"[Task {task_id}] Step {step}/{total}: {message}")

    def _on_task_event(self, event: dict) -> None:
        """Translate a real orchestrator lifecycle change into IPC events."""
        orchestrator = self._components.get("task_orchestrator")
        if orchestrator is None:
            return

        active = orchestrator.has_active_tasks()
        current = orchestrator.get_current_task()
        history = orchestrator.task_history_snapshot(limit=10)
        self._publish("task_history_updated", {
            "reason": event.get("reason", "updated"),
            "active_task": active,
            "current_task": (
                orchestrator.public_task_snapshot(current) if current else None
            ),
            "task_history": history,
        })

        if self._runtime_state is not None:
            if active:
                goal = current.goal[:100] if current else "task active"
                self._runtime_state.set_agent_state("working", goal)
            else:
                self._runtime_state.set_agent_state("idle", "no active task")

    def _on_drive_update(self, state: dict) -> None:
        active = bool(state.get("active"))
        goal = str(state.get("goal", ""))
        action = str(state.get("action", ""))

        if active:
            if not self._drive_was_active:
                self._publish("drive_started", {"goal": goal, "action": action})
            elif action != self._drive_last_action:
                self._publish("drive_progress", {"goal": goal, "action": action})
            self._drive_last_action = action
            self._drive_was_active = True
            print(f"[Drive] {goal}: {action}")
            return

        if self._drive_was_active:
            reason = str(state.get("reason", "done"))
            self._publish(self._drive_terminal_event(reason), {
                "goal": goal,
                "reason": reason,
            })
        self._drive_was_active = False
        self._drive_last_action = ""
        if state.get("reason"):
            print(f"[Drive] Stopped: {state.get('reason')}")

    @staticmethod
    def _drive_terminal_event(reason: str) -> str:
        lowered = reason.lower()
        if any(word in lowered for word in ("cancel", "shutdown", "stopped")):
            return "drive_cancelled"
        if any(word in lowered for word in ("fail", "error", "abort", "denied")):
            return "drive_failed"
        return "drive_completed"

    def _on_screen_context_update(self, frame) -> None:
        self._publish("screen_context_updated", self._screen_snapshot(frame))

    def _on_audio_state_change(self, status) -> None:
        import time

        state = status.state
        if state == "capturing":
            if self._runtime_state is not None:
                self._runtime_state.set_audio_state("capturing", f"recording ({status.device_name})")
                self._runtime_state.set_assistant_state("listening", "push-to-talk active")
            self._publish("audio_capture_started", {
                "device_name": status.device_name,
                "timestamp": round(time.time(), 3),
            })
        elif state == "idle":
            if self._runtime_state is not None:
                self._runtime_state.set_audio_state("idle", "microphone ready")
                self._runtime_state.set_assistant_state("idle", "capture completed")
            self._publish("audio_capture_stopped", {
                "timestamp": round(time.time(), 3),
            })
        elif state == "error":
            if self._runtime_state is not None:
                self._runtime_state.set_audio_state("error", status.error_message)
            self._publish("audio_capture_error", {
                "error": status.error_message,
                "timestamp": round(time.time(), 3),
            })

    def _on_audio_complete(self, capture) -> None:
        ingress = self._components.get("audio_ingress")
        if ingress is not None:
            ingress.submit(capture)
        self._publish("audio_capture_completed", capture.metadata())

    def _on_stt_status_change(self, status) -> None:
        self._publish("stt_status_changed", status.to_dict())

    def _on_stt_started(self, capture_id: str) -> None:
        if self._runtime_state is not None:
            self._runtime_state.set_assistant_state("thinking", "transcribing speech")
        self._publish("stt_transcription_started", {"capture_id": capture_id})

    def _on_stt_completed(self, result) -> None:
        if self._runtime_state is not None:
            self._runtime_state.set_assistant_state("idle", "transcription ready")
        # Publish transcript event so both user_transcript and stt_transcription_completed fire
        self._publish("user_transcript", {
            "text": result.text,
            "language": result.language,
            "duration": result.duration,
            "capture_id": result.capture_id,
        })
        self._publish("stt_transcription_completed", result.to_dict())

        # Forward completed transcript to the conversation manager
        conv = self._components.get("conversation_manager")
        if conv is not None and result.text:
            conv.submit_transcript(result)

    def _on_stt_failed(self, error: str) -> None:
        if self._runtime_state is not None:
            self._runtime_state.set_assistant_state("idle", "transcription failed")
        self._publish("stt_transcription_failed", {"error": error})

    # ── Conversation callbacks ─────────────────────────────────────────────

    def _on_conversation_state_change(self, status) -> None:
        state = status.state
        if state == "thinking":
            if self._runtime_state is not None:
                self._runtime_state.set_assistant_state("thinking", "formulating response")
        elif state == "streaming":
            if self._runtime_state is not None:
                self._runtime_state.set_assistant_state("thinking", "responding")
        elif state in ("completed", "idle"):
            if self._runtime_state is not None and self._runtime_state.snapshot()["assistant_state"] == "thinking":
                self._runtime_state.set_assistant_state("idle", "response ready")
        self._publish("conversation_state_changed", status.to_dict())

    def _on_conversation_user_message(self, message) -> None:
        self._publish("conversation_user_message", message.to_dict())

    def _on_conversation_response_started(self, message_id: str, conversation_id: str) -> None:
        if self._runtime_state is not None:
            self._runtime_state.set_assistant_state("thinking", "generating answer")
        self._publish("conversation_response_started", {
            "message_id": message_id,
            "conversation_id": conversation_id,
        })

    def _on_conversation_response_delta(self, delta: str, accumulated: str) -> None:
        self._publish("conversation_response_delta", {
            "delta": delta,
            "accumulated": accumulated,
        })

    def _on_conversation_response_completed(self, message) -> None:
        if self._runtime_state is not None:
            self._runtime_state.set_assistant_state("idle", "response complete")
        self._publish("assistant_response", {"content": message.content})
        self._publish("conversation_response_completed", message.to_dict())

    def _on_conversation_response_failed(self, error: str) -> None:
        if self._runtime_state is not None:
            self._runtime_state.set_assistant_state("idle", "response failed")
        self._publish("conversation_response_failed", {"error": error})

    def _on_conversation_cancelled(self) -> None:
        if self._runtime_state is not None:
            self._runtime_state.set_assistant_state("idle", "generation cancelled")
        self._publish("conversation_cancelled", {})

    def _on_agent_tool_started(self, tool_name: str, call_id: str) -> None:
        if self._runtime_state is not None:
            self._runtime_state.set_assistant_state("thinking", f"checking {tool_name}")
        self._publish("agent_tool_started", {
            "tool_name": tool_name,
            "tool_call_id": call_id,
        })

    def _on_agent_tool_completed(self, result) -> None:
        self._publish("agent_tool_completed", result.to_dict())

    def _on_command_approval_requested(self, req_dict: dict) -> None:
        if self._runtime_state is not None:
            self._runtime_state.set_assistant_state("thinking", "awaiting command approval")
        self._publish("command_approval_requested", req_dict)

    def _on_command_approval_resolved(self, res_dict: dict) -> None:
        self._publish("command_approval_resolved", res_dict)

    def _on_media_status_changed(self, snapshot) -> None:
        self._publish("media_status_changed", snapshot.to_dict())

    def _on_media_lyrics_loaded(self, lyrics) -> None:
        self._publish("media_lyrics_loaded", lyrics.to_dict())

    def _on_telemetry_snapshot(self, snapshot) -> None:
        self._publish("telemetry_snapshot_changed", snapshot.to_dict())

    def _on_mic_state_changed(self, state) -> None:
        self._publish("mic_state_changed", state.to_dict())

    def _on_clip_history_changed(self, items) -> None:
        self._publish("clip_history_changed", {"count": len(items)})

    def _on_shelf_items_changed(self, items) -> None:
        self._publish("shelf_items_changed", {"count": len(items)})

    def _on_bridge_status_changed(self, status) -> None:
        self._publish("bridge_status_changed", status.to_dict())

    def _on_bridge_pairing_requested(self, req_dict) -> None:
        self._publish("bridge_pairing_requested", req_dict)

    def _on_bridge_transfer_event(self, event: str, payload: dict) -> None:
        if event in (
            "bridge_transfer_requested",
            "bridge_transfer_progress",
            "bridge_transfer_completed",
            "bridge_download_requested",
            "bridge_download_progress",
            "bridge_download_completed",
        ):
            self._publish(event, payload)

    @staticmethod
    def _screen_snapshot(frame) -> dict:
        """UI-safe context metadata: no OCR text and no captured image."""
        if frame is None:
            return {
                "cleared": True,
                "active_app": "",
                "active_title": "",
                "is_terminal": False,
                "is_browser": False,
                "source": "",
                "age_seconds": 0.0,
            }
        return {
            "cleared": False,
            "active_app": str(getattr(frame, "active_app", "") or ""),
            "active_title": str(getattr(frame, "active_title", "") or ""),
            "is_terminal": bool(getattr(frame, "is_terminal", False)),
            "is_browser": bool(getattr(frame, "is_browser", False)),
            "source": str(getattr(frame, "source", "") or ""),
            "age_seconds": round(float(frame.age()), 3),
        }

    # ── Run / shutdown ─────────────────────────────────────────────────────

    def run(self) -> int:
        """Wait until SIGINT/SIGTERM (or an explicit shutdown) arrives."""
        if not self._initialized:
            print("KIVO backend is not initialized.")
            return 1
        if self._shutdown_started:
            return 0

        self._running = True
        print("KIVO backend running. Press Ctrl+C to stop.")
        self._shutdown_event.wait()
        print("Shutdown complete.")
        return 0

    def shutdown(self) -> None:
        """Release every component exactly once.

        Safe to call after a partial startup, from a signal handler, or twice.
        """
        if self._shutdown_started:
            self._shutdown_event.wait(timeout=5.0)
            return
        self._shutdown_started = True
        self._running = False
        print("Shutting down KIVO backend...")

        # Let in-flight task steps see the cancellation before their engine
        # disappears. The still-running IPC server receives the resulting
        # Drive cancellation event.
        orchestrator = self._components.get("task_orchestrator")
        if orchestrator is not None:
            try:
                orchestrator.cancel_active()
            except Exception:
                pass
            try:
                orchestrator.remove_listener(self._task_listener)
            except Exception:
                pass
            try:
                orchestrator.set_progress_callback(None)
            except Exception:
                pass

        drive = self._components.get("drive")
        if drive is not None:
            try:
                drive.stop("shutdown")
            except Exception:
                pass
            try:
                drive.remove_listener(self._drive_listener)
            except Exception:
                pass

        screen = self._components.get("screen_context")
        if screen is not None:
            try:
                screen.remove_listener(self._screen_listener)
            except Exception:
                pass

        confirm = self._components.get("confirm_gate")
        if confirm is not None:
            try:
                confirm.bind(None, None, None)
            except Exception:
                pass

        # Stop conversation manager and cancel in-flight requests
        conv_mgr = self._components.get("conversation_manager")
        if conv_mgr is not None:
            try:
                conv_mgr.close()
            except Exception:
                pass

        # The application owns final shutdown of the filesystem worker. The
        # conversation manager has already cancelled active work and approvals.
        file_worker = self._components.get("file_worker")
        if file_worker is not None:
            try:
                file_worker.close()
            except Exception:
                pass

        # Stop STT background worker and unload model
        stt_mgr = self._components.get("transcription_manager")
        if stt_mgr is not None:
            try:
                stt_mgr.stop()
            except Exception:
                pass

        # Close active audio capture and clear buffers
        audio_cm = self._components.get("audio_capture_manager")
        if audio_cm is not None:
            try:
                audio_cm.close()
            except Exception:
                pass

        audio_ingress = self._components.get("audio_ingress")
        if audio_ingress is not None:
            try:
                audio_ingress.clear()
            except Exception:
                pass

        # Stop MPRIS media monitoring and close lyrics client
        media = self._components.get("media_service")
        if media is not None:
            try:
                media.close_sync()
            except Exception:
                pass

        # Stop telemetry before the IPC server disappears. No telemetry event
        # is published in Stage 12.2.1, but later stages rely on this order.
        telemetry = self._components.get("telemetry_service")
        if telemetry is not None:
            try:
                telemetry.close(2.0)
            except Exception:
                pass

        # Stop microphone service before IPC server disappears. Never unmutes.
        mic = self._components.get("microphone_service")
        if mic is not None:
            try:
                mic.close(3.0)
            except Exception:
                pass

        # Stop clipboard service before IPC server disappears.
        clipboard = self._components.get("clipboard_service")
        if clipboard is not None:
            try:
                clipboard.close(2.0)
            except Exception:
                pass

        # Stop file shelf service before IPC server disappears.
        shelf = self._components.get("shelf_service")
        if shelf is not None:
            try:
                shelf.close()
            except Exception:
                pass

        # Stop phone bridge service before IPC server disappears.
        bridge = self._components.get("bridge_service")
        if bridge is not None:
            try:
                bridge.close(3.0)
            except Exception:
                pass

        # Stop accepting clients, close connections and remove the socket.
        ipc = self._components.get("ipc")
        if ipc is not None:
            try:
                ipc.stop()
            except Exception:
                pass
        self._ipc = None

        runtime = self._components.get("runtime_state")
        if runtime is not None:
            try:
                runtime.remove_listener(self._runtime_listener)
            except Exception:
                pass
        self._runtime_state = None

        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=2.0)
        self._threads.clear()

        self._components.clear()
        self._shutdown_event.set()


def run() -> int:
    """Create the application, initialize it and run until shutdown."""
    app = KivoApp()
    app.setup_signal_handlers()

    if not app.initialize():
        return 1
    return app.run()


if __name__ == "__main__":
    sys.exit(run())
