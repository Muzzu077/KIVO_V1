"""Phone Bridge service orchestrating local pairing and ephemeral server lifecycle."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from kivo.bridge.crypto import generate_ephemeral_certificate
from kivo.bridge.download import (
    DownloadError,
    DownloadManager,
    public_download,
)
from kivo.bridge.download_validation import (
    DownloadValidationError,
    validate_authorize_flag,
    validate_shelf_item_id,
)
from kivo.bridge.models import (
    BridgeState,
    BridgeStatus,
    PairingOffer,
)
from kivo.bridge.net import discover_lan_interface, find_available_port
from kivo.bridge.pairing import PairingManager
from kivo.bridge.quarantine import QuarantineError, QuarantineManager
from kivo.bridge.server import BridgeServer
from kivo.bridge.transfer import (
    TransferError,
    TransferManager,
    public_transfer,
)
from kivo.shelf.path_policy import ShelfPolicyError


class PhoneBridgeService:
    """Manages ephemeral local-network server, QR pairing, and device authorization."""

    def __init__(
        self,
        on_status_changed: Callable[[BridgeStatus], None] | None = None,
        on_pairing_requested: Callable[[dict[str, Any]], None] | None = None,
        on_transfer_event: Callable[[str, dict[str, Any]], None] | None = None,
        quarantine: QuarantineManager | None = None,
        shelf_service: Any | None = None,
    ):
        self._on_status_changed = on_status_changed
        self._on_pairing_requested = on_pairing_requested
        self._on_transfer_event = on_transfer_event
        self._shelf_service = shelf_service

        self._pairing_manager = PairingManager()
        self._quarantine = quarantine if quarantine is not None else QuarantineManager()
        self._transfer_manager = TransferManager(
            session_provider=lambda: self._pairing_manager.active_session,
            quarantine=self._quarantine,
            on_event=self._on_transfer_event_internal,
        )
        self._download_manager = DownloadManager(
            session_provider=lambda: self._pairing_manager.active_session,
            on_event=self._on_transfer_event_internal,
        )
        self._server: BridgeServer | None = None
        self._lan_ip: str | None = None
        self._port: int | None = None
        self._cert_fp: str | None = None
        self._closed = False
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._server is not None and self._server._running

    def get_status(self) -> BridgeStatus:
        """Return current status of the bridge."""
        with self._lock:
            state = self._pairing_manager.state
            offer = self._pairing_manager.current_offer
            pending = self._pairing_manager.pending_request
            active = self._pairing_manager.active_session

            return BridgeStatus(
                state=state.value,
                lan_ip=self._lan_ip,
                port=self._port,
                cert_fingerprint=self._cert_fp,
                qr_data_url=offer.qr_data_url if offer else None,
                pending_pairing=pending.to_dict() if pending else None,
                active_device=active.to_dict() if active else None,
            )

    def start_bridge(self, preferred_port: int = 8443) -> dict[str, Any]:
        """Discover physical LAN interface, generate ephemeral TLS cert, and start server."""
        with self._lock:
            if self._closed:
                return {"ok": False, "error": "Bridge service is closed"}
            if self._server is not None and self._server._running:
                return {"ok": True, "status": self.get_status().to_dict()}

            # 1. Discover physical LAN interface
            iface_info = discover_lan_interface()
            if not iface_info:
                return {
                    "ok": False,
                    "error": "No active physical LAN interface discovered (Wi-Fi or Ethernet required)",
                }
            iface_name, lan_ip = iface_info

            # 2. Allocate port
            port = find_available_port(lan_ip, preferred=preferred_port)

            # 3. Generate ephemeral ECDSA P-256 certificate
            try:
                cert, key, fp_hex, cert_pem, key_pem = generate_ephemeral_certificate(lan_ip)
            except Exception as exc:
                return {"ok": False, "error": f"Failed to generate TLS certificate: {exc}"}

            self._lan_ip = lan_ip
            self._port = port
            self._cert_fp = fp_hex

            # 4. Initialize quarantined upload storage before accepting clients.
            try:
                self._transfer_manager.start()
                self._download_manager.start()
            except (QuarantineError, TransferError, DownloadError) as exc:
                try:
                    self._download_manager.stop("startup_failed")
                except Exception:
                    pass
                self._lan_ip = None
                self._port = None
                self._cert_fp = None
                return {"ok": False, "error": f"Upload storage unavailable: {exc}"}

            # 5. Create pairing offer
            offer = self._pairing_manager.create_offer(lan_ip, port, fp_hex)

            # 6. Start ephemeral Uvicorn server
            server = BridgeServer(
                pairing_manager=self._pairing_manager,
                lan_ip=lan_ip,
                port=port,
                cert_pem=cert_pem,
                key_pem=key_pem,
                on_pairing_requested=self._on_client_pairing_requested,
                transfer_manager=self._transfer_manager,
                download_manager=self._download_manager,
            )
            server.start()
            self._server = server

        status = self.get_status()
        self._notify_status(status)
        return {"ok": True, "status": status.to_dict()}

    def stop_bridge(self) -> bool:
        """Stop ephemeral server, cancel transfers, and reset pairing state."""
        with self._lock:
            try:
                self._download_manager.stop("bridge_stopped")
            except Exception:
                pass
            try:
                self._transfer_manager.stop("bridge_stopped")
            except Exception:
                pass
            if self._server is not None:
                self._server.stop()
                self._server = None
            self._pairing_manager.reset_locked()
            self._lan_ip = None
            self._port = None
            self._cert_fp = None
            try:
                self._quarantine.cleanup_abandoned()
            except Exception:
                pass

        status = self.get_status()
        self._notify_status(status)
        return True

    def approve_pairing(self, session_id: str) -> bool:
        """Approve pending pairing request on desktop."""
        ok, session = self._pairing_manager.approve_pairing(session_id)
        if ok:
            self._notify_status(self.get_status())
        return ok

    def reject_pairing(self, session_id: str) -> bool:
        """Reject pending pairing request on desktop."""
        ok = self._pairing_manager.reject_pairing(session_id)
        if ok:
            self._notify_status(self.get_status())
        return ok

    def revoke_device(self, session_id: str) -> bool:
        """Revoke active device session and cancel that device's transfers."""
        ok = self._pairing_manager.revoke_device(session_id)
        if ok:
            try:
                self._transfer_manager.cancel_session(session_id, "session_revoked")
            except Exception:
                pass
            try:
                self._download_manager.cancel_session(session_id, "session_revoked")
            except Exception:
                pass
            self._notify_status(self.get_status())
        return ok

    def get_transfers(self) -> dict[str, Any]:
        """Return a bounded, path-free snapshot for local IPC clients."""
        try:
            return {"ok": True, "transfers": self._transfer_manager.list_public()}
        except Exception as exc:  # noqa: BLE001 - IPC must stay structured
            return {"ok": False, "error_code": "transfer_unavailable", "error_message": str(exc)}

    def resolve_transfer(self, transfer_id: str, decision: str) -> dict[str, Any]:
        """Apply one documented desktop decision; never accepts paths."""
        try:
            transfer = self._transfer_manager.resolve(transfer_id, decision)
            return {"ok": True, "transfer": public_transfer(transfer)}
        except TransferError as exc:
            return {
                "ok": False,
                "error_code": exc.code,
                "error_message": exc.message,
                "details": exc.details,
            }
        except Exception as exc:  # noqa: BLE001 - IPC must stay structured
            return {
                "ok": False,
                "error_code": "transfer_failed",
                "error_message": str(exc),
            }

    def cancel_transfer(self, transfer_id: str) -> dict[str, Any]:
        try:
            transfer = self._transfer_manager.cancel(transfer_id)
            return {"ok": True, "transfer": public_transfer(transfer)}
        except TransferError as exc:
            return {
                "ok": False,
                "error_code": exc.code,
                "error_message": exc.message,
                "details": exc.details,
            }
        except Exception as exc:  # noqa: BLE001 - IPC must stay structured
            return {
                "ok": False,
                "error_code": "transfer_failed",
                "error_message": str(exc),
            }

    # ── Desktop-authorized downloads ───────────────────────────────────────

    def create_download_ticket(
        self, item_id: object, authorize: object
    ) -> dict[str, Any]:
        """Explicitly authorize one File Shelf item for export to the phone."""
        if self._shelf_service is None:
            return {
                "ok": False,
                "error_code": "shelf_unavailable",
                "error_message": "File Shelf service is unavailable",
            }
        try:
            validate_authorize_flag(authorize)
            safe_item_id = validate_shelf_item_id(item_id)
        except DownloadValidationError as exc:
            return {"ok": False, "error_code": exc.code, "error_message": exc.message}

        try:
            grant = self._shelf_service.authorize_export(safe_item_id)
        except ShelfPolicyError as exc:
            return {"ok": False, "error_code": exc.code, "error_message": exc.message}
        except Exception as exc:  # noqa: BLE001 - IPC must stay structured
            return {
                "ok": False,
                "error_code": "authorization_failed",
                "error_message": str(exc),
            }

        try:
            ticket = self._download_manager.create_ticket(grant)
            return {"ok": True, "download": public_download(ticket)}
        except DownloadError as exc:
            return {
                "ok": False,
                "error_code": exc.code,
                "error_message": exc.message,
                "details": exc.details,
            }
        except Exception as exc:  # noqa: BLE001 - IPC must stay structured
            grant.close()
            return {
                "ok": False,
                "error_code": "download_failed",
                "error_message": str(exc),
            }

    def get_downloads(self) -> dict[str, Any]:
        try:
            return {"ok": True, "downloads": self._download_manager.list_public()}
        except Exception as exc:  # noqa: BLE001 - IPC must stay structured
            return {
                "ok": False,
                "downloads": [],
                "error_code": "download_unavailable",
                "error_message": str(exc),
            }

    def cancel_download(self, download_id: str) -> dict[str, Any]:
        try:
            ticket = self._download_manager.cancel_desktop(download_id)
            return {"ok": True, "download": public_download(ticket)}
        except DownloadError as exc:
            return {
                "ok": False,
                "error_code": exc.code,
                "error_message": exc.message,
                "details": exc.details,
            }
        except Exception as exc:  # noqa: BLE001 - IPC must stay structured
            return {
                "ok": False,
                "error_code": "download_failed",
                "error_message": str(exc),
            }

    def close(self, timeout: float = 3.0) -> None:
        """Orderly service shutdown: cancel transfers, stop server, close roots."""
        self._closed = True
        self.stop_bridge()
        try:
            self._download_manager.close("bridge_stopped")
        except Exception:
            pass
        try:
            self._transfer_manager.close("bridge_stopped")
        except Exception:
            pass

    def _on_client_pairing_requested(self, request_dict: dict[str, Any]) -> None:
        self._notify_status(self.get_status())
        if self._on_pairing_requested:
            try:
                self._on_pairing_requested(request_dict)
            except Exception:
                pass

    def _on_transfer_event_internal(self, event: str, payload: dict[str, Any]) -> None:
        if self._on_transfer_event and not self._closed:
            try:
                self._on_transfer_event(event, payload)
            except Exception:
                pass

    def _notify_status(self, status: BridgeStatus) -> None:
        if self._on_status_changed and not self._closed:
            try:
                self._on_status_changed(status)
            except Exception:
                pass


__all__ = ["PhoneBridgeService"]
