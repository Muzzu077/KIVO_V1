"""Asynchronous MPRIS MediaService for player discovery, controls, and status.

Metadata is read one field at a time from ``playerctl``.  Free-text fields are
never concatenated with a delimiter, so titles containing delimiter-like text,
newlines, Unicode or emoji cannot shift or corrupt adjacent fields.  The only
delimited read contains the constrained status enum and two numeric fields.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import selectors
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from kivo.media.artwork import ArtworkResolver
from kivo.media.lyrics import LyricsClient, LyricsResult

_PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,254}$")
_NUMERIC_FORMAT = "{{status}}|{{position}}|{{mpris:length}}"
_TEXT_METADATA_KEYS = (
    "xesam:artist",
    "xesam:title",
    "xesam:album",
    "mpris:artUrl",
)
_VALID_STATUSES = {"Playing", "Paused", "Stopped"}
_MAX_PLAYERS = 16
_MAX_PLAYERCTL_OUTPUT_BYTES = 256 * 1024
_DEFAULT_COMMAND_TIMEOUT_SECONDS = 1.0


@dataclass
class MediaSnapshot:
    """Current state of active media playback."""

    available: bool = False
    player_name: str = ""
    players: list[str] = field(default_factory=list)
    status: str = "Unavailable"  # "Playing" | "Paused" | "Stopped" | "Unavailable"
    title: str = ""
    artist: str = ""
    album: str = ""
    art_url: str = ""
    position_seconds: float = 0.0
    duration_seconds: float = 0.0
    can_seek: bool = True
    can_play_pause: bool = True
    can_go_next: bool = True
    can_go_previous: bool = True
    lyrics_available: bool = False
    # Untrusted remote URL awaiting bounded download.  Never serialized.
    pending_art_url: str = field(default="", repr=False, compare=False)

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "player_name": self.player_name,
            "players": list(self.players),
            "status": self.status,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "art_url": self.art_url,
            "position_seconds": round(self.position_seconds, 2),
            "duration_seconds": round(self.duration_seconds, 2),
            "can_seek": self.can_seek,
            "can_play_pause": self.can_play_pause,
            "can_go_next": self.can_go_next,
            "can_go_previous": self.can_go_previous,
            "lyrics_available": self.lyrics_available,
        }


def _parse_non_negative_float(raw: str) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(value) or value < 0:
        return 0.0
    return value


def _normalize_status(raw: str) -> Optional[str]:
    value = str(raw or "").strip().capitalize()
    return value if value in _VALID_STATUSES else None


class MediaService:
    """Controls and observes MPRIS-compatible media players via playerctl."""

    def __init__(
        self,
        on_status_changed: Optional[Callable[[MediaSnapshot], None]] = None,
        on_lyrics_loaded: Optional[Callable[[LyricsResult], None]] = None,
        lyrics_client: Optional[LyricsClient] = None,
        artwork_resolver: Optional[ArtworkResolver] = None,
        command_timeout_seconds: float = _DEFAULT_COMMAND_TIMEOUT_SECONDS,
        playerctl_path: Optional[str] = None,
    ):
        self._on_status_changed = on_status_changed
        self._on_lyrics_loaded = on_lyrics_loaded
        self._lyrics_client = lyrics_client or LyricsClient()
        self._owns_lyrics = lyrics_client is None
        self._artwork = artwork_resolver or ArtworkResolver()
        self._owns_artwork = artwork_resolver is None
        self._command_timeout = max(0.1, float(command_timeout_seconds))
        self._playerctl_path = (
            playerctl_path if playerctl_path is not None else shutil.which("playerctl")
        )

        self._snapshot = MediaSnapshot()
        self._position_reference: float = 0.0
        self._position_updated_at: float = 0.0
        self._last_lyrics: Optional[LyricsResult] = None
        self._lyrics_request_id: int = 0
        self._lyrics_loaded_key: tuple[str, str] = ("", "")
        self._lyrics_task: Optional[asyncio.Task] = None

        self._art_request_id: int = 0
        self._art_loaded_url: str = ""
        self._art_pending_url: str = ""
        self._art_task: Optional[asyncio.Task] = None

        self._running = False
        self._closed = False
        self._clients_closed = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_ready = threading.Event()
        self._control_lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    # ── Public state ───────────────────────────────────────────────────────

    @property
    def snapshot(self) -> MediaSnapshot:
        return self._snapshot

    @property
    def last_lyrics(self) -> Optional[LyricsResult]:
        return self._last_lyrics

    def get_current_position(self) -> float:
        """Return the current playback position interpolated with monotonic time."""
        if self._snapshot.status == "Playing" and self._position_updated_at > 0:
            elapsed = time.monotonic() - self._position_updated_at
            current = self._position_reference + max(0.0, elapsed)
            if self._snapshot.duration_seconds > 0:
                current = min(current, self._snapshot.duration_seconds)
            return round(max(0.0, current), 2)
        return round(max(0.0, self._position_reference), 2)

    def get_status_dict(self) -> dict:
        if not self._snapshot.available and not self._closed:
            self.refresh_sync()
        snap_dict = self._snapshot.to_dict()
        snap_dict["position_seconds"] = self.get_current_position()
        return snap_dict

    def get_lyrics_dict(self) -> dict:
        if self._last_lyrics is not None:
            return self._last_lyrics.to_dict()
        return {
            "track": self._snapshot.title,
            "artist": self._snapshot.artist,
            "synced": False,
            "lines": [],
        }

    # ── Player discovery and field collection ──────────────────────────────

    @staticmethod
    def _valid_player_names(output: str) -> list[str]:
        names: list[str] = []
        for line in str(output or "").splitlines():
            name = line.strip()
            if not name or not _PLAYER_NAME_RE.fullmatch(name):
                continue
            if name not in names:
                names.append(name)
            if len(names) >= _MAX_PLAYERS:
                break
        return names

    async def _list_players(self) -> list[str]:
        code, output = await self._run_playerctl("-l")
        if code != 0:
            return []
        return self._valid_player_names(output)

    def _list_players_sync(self) -> list[str]:
        code, output = self._run_playerctl_sync("-l")
        if code != 0:
            return []
        return self._valid_player_names(output)

    async def _read_player_async(self, player: str) -> Optional[MediaSnapshot]:
        code, output = await self._run_playerctl(
            "-p", player, "metadata", "--format", _NUMERIC_FORMAT
        )
        if code != 0:
            return None
        parts = str(output).split("|")
        if len(parts) != 3:
            return None
        status_raw, position_raw, length_raw = parts

        text: dict[str, str] = {}
        for key in _TEXT_METADATA_KEYS:
            field_code, value = await self._run_playerctl(
                "-p", player, "metadata", key
            )
            text[key] = value if field_code == 0 else ""
        return self._snapshot_from_fields(
            player,
            status_raw,
            text["xesam:artist"],
            text["xesam:title"],
            text["xesam:album"],
            text["mpris:artUrl"],
            length_raw,
            position_raw,
        )

    def _read_player_sync(self, player: str) -> Optional[MediaSnapshot]:
        code, output = self._run_playerctl_sync(
            "-p", player, "metadata", "--format", _NUMERIC_FORMAT
        )
        if code != 0:
            return None
        parts = str(output).split("|")
        if len(parts) != 3:
            return None
        status_raw, position_raw, length_raw = parts

        text: dict[str, str] = {}
        for key in _TEXT_METADATA_KEYS:
            field_code, value = self._run_playerctl_sync(
                "-p", player, "metadata", key
            )
            text[key] = value if field_code == 0 else ""
        return self._snapshot_from_fields(
            player,
            status_raw,
            text["xesam:artist"],
            text["xesam:title"],
            text["xesam:album"],
            text["mpris:artUrl"],
            length_raw,
            position_raw,
        )

    def _snapshot_from_fields(
        self,
        player_name: str,
        status: str,
        artist: str,
        title: str,
        album: str,
        art_url: str,
        length_raw: str,
        position_raw: str,
    ) -> Optional[MediaSnapshot]:
        """Build a snapshot from separately-read fields.

        No delimiter or line framing is applied to artist/title/album/art, so
        delimiter text, embedded newlines and Unicode remain field-local.
        """
        normalized_status = _normalize_status(status)
        if normalized_status is None:
            return None
        artwork = self._artwork.prepare(str(art_url or ""))
        duration_seconds = _parse_non_negative_float(length_raw) / 1_000_000.0
        position_seconds = _parse_non_negative_float(position_raw) / 1_000_000.0
        return MediaSnapshot(
            available=True,
            player_name=str(player_name or ""),
            players=[player_name] if player_name else [],
            status=normalized_status,
            title=str(title or ""),
            artist=str(artist or ""),
            album=str(album or ""),
            art_url=artwork.display_url,
            pending_art_url=artwork.remote_url,
            position_seconds=position_seconds,
            duration_seconds=duration_seconds,
        )

    @staticmethod
    def _select_snapshot(snapshots: list[MediaSnapshot]) -> Optional[MediaSnapshot]:
        if not snapshots:
            return None
        playing = [snap for snap in snapshots if snap.status == "Playing"]
        return playing[0] if playing else snapshots[0]

    async def refresh(self) -> MediaSnapshot:
        """Query active players and update the internal snapshot."""
        players = await self._list_players()
        if not players:
            return self._mark_unavailable()

        snapshots: list[MediaSnapshot] = []
        for player in players:
            try:
                snapshot = await self._read_player_async(player)
            except Exception:
                snapshot = None
            if snapshot is not None:
                snapshots.append(snapshot)
        selected = self._select_snapshot(snapshots)
        if selected is None:
            return self._mark_unavailable()
        selected.players = list(players)
        self._update_snapshot(selected)
        return self._snapshot

    def refresh_sync(self) -> MediaSnapshot:
        """Synchronously query active players via playerctl."""
        with self._refresh_lock:
            players = self._list_players_sync()
            if not players:
                return self._mark_unavailable()

            snapshots: list[MediaSnapshot] = []
            for player in players:
                try:
                    snapshot = self._read_player_sync(player)
                except Exception:
                    snapshot = None
                if snapshot is not None:
                    snapshots.append(snapshot)
            selected = self._select_snapshot(snapshots)
            if selected is None:
                return self._mark_unavailable()
            selected.players = list(players)
            self._update_snapshot_sync(selected)
            return self._snapshot

    # ── Snapshot transitions ───────────────────────────────────────────────

    @staticmethod
    def _track_key(snapshot: MediaSnapshot) -> tuple[str, str]:
        return (
            snapshot.artist.strip().lower(),
            snapshot.title.strip().lower(),
        )

    def _update_snapshot(self, new_snap: MediaSnapshot) -> None:
        previous = self._snapshot
        track_changed = self._track_key(new_snap) != self._track_key(previous)
        player_changed = new_snap.player_name != previous.player_name
        status_changed = new_snap.status != previous.status
        became_available = new_snap.available != previous.available

        self._snapshot = new_snap
        self._position_reference = new_snap.position_seconds
        self._position_updated_at = time.monotonic()

        if track_changed or player_changed:
            self._cancel_task(self._lyrics_task)
            self._lyrics_task = None
            self._last_lyrics = None
            self._lyrics_loaded_key = ("", "")
            self._lyrics_request_id += 1
            self._emit_empty_lyrics(new_snap)

            self._cancel_task(self._art_task)
            self._art_task = None
            self._art_loaded_url = ""
            self._art_pending_url = ""
            self._art_request_id += 1

        self._ensure_lyrics(new_snap)
        self._ensure_artwork(new_snap)

        if track_changed or player_changed or status_changed or became_available:
            self._emit_status()

    def _update_snapshot_sync(self, new_snap: MediaSnapshot) -> None:
        previous = self._snapshot
        track_changed = self._track_key(new_snap) != self._track_key(previous)
        player_changed = new_snap.player_name != previous.player_name
        status_changed = new_snap.status != previous.status
        became_available = new_snap.available != previous.available

        self._snapshot = new_snap
        self._position_reference = new_snap.position_seconds
        self._position_updated_at = time.monotonic()

        if track_changed or player_changed:
            self._last_lyrics = None
            self._lyrics_loaded_key = ("", "")
            self._lyrics_request_id += 1
            self._emit_empty_lyrics(new_snap)
            self._art_loaded_url = ""
            self._art_pending_url = ""
            self._art_request_id += 1

        if track_changed or player_changed or status_changed or became_available:
            self._emit_status()

    def _mark_unavailable(self) -> MediaSnapshot:
        was_available = self._snapshot.available
        had_lyrics = self._last_lyrics is not None
        self._cancel_task(self._lyrics_task)
        self._lyrics_task = None
        self._cancel_task(self._art_task)
        self._art_task = None
        self._lyrics_request_id += 1
        self._art_request_id += 1
        self._snapshot = MediaSnapshot(available=False, status="Unavailable")
        self._position_reference = 0.0
        self._position_updated_at = 0.0
        self._last_lyrics = None
        self._lyrics_loaded_key = ("", "")
        self._art_loaded_url = ""
        self._art_pending_url = ""
        if was_available or had_lyrics:
            self._emit_status()
            self._emit_empty_lyrics(self._snapshot)
        return self._snapshot

    def _emit_status(self) -> None:
        if self._closed or self._on_status_changed is None:
            return
        try:
            self._on_status_changed(self._snapshot)
        except Exception:
            pass

    def _emit_empty_lyrics(self, snapshot: MediaSnapshot) -> None:
        if self._closed or self._on_lyrics_loaded is None:
            return
        empty = LyricsResult(
            track=snapshot.title,
            artist=snapshot.artist,
            synced=False,
            lines=[],
        )
        try:
            self._on_lyrics_loaded(empty)
        except Exception:
            pass

    # ── Background lyrics / artwork enrichment ─────────────────────────────

    def _spawn(self, coro) -> Optional[asyncio.Task]:
        if self._closed:
            coro.close()
            return None
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:
            coro.close()
            return None
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    @staticmethod
    def _cancel_task(task: Optional[asyncio.Task]) -> None:
        if task is None or task.done():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            # Called from a synchronous IPC thread; the request-id guard will
            # discard the result without cross-thread cancellation.
            return
        if running is task.get_loop():
            task.cancel()

    def _ensure_lyrics(self, snapshot: MediaSnapshot) -> None:
        key = self._track_key(snapshot)
        if not key[0] or not key[1] or self._closed:
            return
        if key == self._lyrics_loaded_key:
            return
        if self._lyrics_task is not None and not self._lyrics_task.done():
            return
        self._lyrics_request_id += 1
        request_id = self._lyrics_request_id
        self._lyrics_task = self._spawn(
            self._fetch_lyrics_async(snapshot.artist, snapshot.title, request_id)
        )

    def _ensure_artwork(self, snapshot: MediaSnapshot) -> None:
        url = snapshot.pending_art_url
        if not url or self._closed or url == self._art_loaded_url:
            return
        if self._art_task is not None and not self._art_task.done():
            return
        self._art_request_id += 1
        request_id = self._art_request_id
        self._art_pending_url = url
        self._art_task = self._spawn(self._fetch_artwork_async(url, request_id))

    async def _fetch_lyrics_async(
        self, artist: str, track: str, request_id: int
    ) -> None:
        key = (artist.strip().lower(), track.strip().lower())
        try:
            lyrics = await self._lyrics_client.get_lyrics(artist, track)
        except Exception:
            lyrics = None
        finally:
            if self._lyrics_task is asyncio.current_task():
                self._lyrics_task = None

        if self._closed or request_id != self._lyrics_request_id:
            return
        if self._track_key(self._snapshot) != key:
            return

        # Mark this key as settled even for "not found" and transient failure
        # so a bad network never causes a once-per-poll retry storm.  A new
        # track (or a restart) naturally gets a fresh request.
        self._lyrics_loaded_key = key
        if lyrics is not None:
            self._last_lyrics = lyrics
            self._snapshot.lyrics_available = True
            if self._on_lyrics_loaded is not None:
                try:
                    self._on_lyrics_loaded(lyrics)
                except Exception:
                    pass

    async def _fetch_artwork_async(self, url: str, request_id: int) -> None:
        try:
            local_url = await self._artwork.fetch_remote(url)
        except Exception:
            local_url = None
        finally:
            if self._art_task is asyncio.current_task():
                self._art_task = None

        if self._closed or request_id != self._art_request_id:
            return
        if self._snapshot.pending_art_url != url:
            return

        self._art_pending_url = ""
        self._snapshot.pending_art_url = ""
        if local_url:
            self._art_loaded_url = url
            self._snapshot.art_url = local_url
            self._emit_status()

    # ── playerctl subprocess execution ─────────────────────────────────────

    async def _run_playerctl(self, *args: str) -> tuple[int, str]:
        # Run the bounded synchronous implementation in a worker thread so no
        # asyncio loop is ever blocked by a misbehaving player or D-Bus call.
        return await asyncio.to_thread(self._run_playerctl_sync, *args)

    def _run_playerctl_sync(self, *args: str) -> tuple[int, str]:
        if not self._playerctl_path:
            return 1, "playerctl not installed"
        try:
            return self._run_bounded_process(
                [self._playerctl_path, *args],
                timeout=self._command_timeout,
                output_limit=_MAX_PLAYERCTL_OUTPUT_BYTES,
            )
        except Exception as exc:
            return 1, str(exc)

    def _run_bounded_process(
        self, argv: list[str], timeout: float, output_limit: int
    ) -> tuple[int, str]:
        """Run a subprocess with a timeout, bounded stdout/stderr and kill."""
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            return 1, str(exc)

        stdout = proc.stdout
        stderr = proc.stderr
        if stdout is None or stderr is None:
            self._terminate_process(proc)
            return 1, "playerctl pipes unavailable"

        stdout_fd = stdout.fileno()
        stderr_fd = stderr.fileno()
        buffers = {stdout_fd: bytearray(), stderr_fd: bytearray()}
        selector = selectors.DefaultSelector()
        deadline = time.monotonic() + max(0.05, float(timeout))
        failure = ""

        try:
            for descriptor in buffers:
                selector.register(descriptor, selectors.EVENT_READ)

            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "playerctl timed out"
                    break
                events = selector.select(timeout=min(remaining, 0.1))
                if not events:
                    if proc.poll() is not None:
                        # The process exited; a final select will reveal EOF.
                        continue
                    continue
                for key, _ in events:
                    descriptor = key.fd
                    try:
                        chunk = os.read(descriptor, 65536)
                    except BlockingIOError:
                        continue
                    except OSError:
                        try:
                            selector.unregister(descriptor)
                        except Exception:
                            pass
                        continue
                    if not chunk:
                        try:
                            selector.unregister(descriptor)
                        except Exception:
                            pass
                        continue
                    buffer = buffers[descriptor]
                    if len(buffer) + len(chunk) > output_limit:
                        failure = "playerctl output too large"
                        break
                    buffer.extend(chunk)
                if failure:
                    break

            if not failure:
                try:
                    remaining = max(0.05, deadline - time.monotonic())
                    return_code = proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    failure = "playerctl timed out"
                    return_code = 1
            else:
                return_code = 1
        finally:
            if failure:
                self._terminate_process(proc)
            try:
                selector.close()
            except Exception:
                pass
            try:
                stdout.close()
            except Exception:
                pass
            try:
                stderr.close()
            except Exception:
                pass

        if failure:
            return 1, failure
        return return_code, bytes(buffers[stdout_fd]).decode(
            "utf-8", errors="replace"
        ).strip()

    @staticmethod
    def _terminate_process(proc: subprocess.Popen) -> None:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=1.0)
        except Exception:
            pass

    # ── Playback Controls ──────────────────────────────────────────────────

    async def play(self) -> None:
        await self._run_playerctl("play")
        await self.refresh()

    async def pause(self) -> None:
        await self._run_playerctl("pause")
        await self.refresh()

    async def play_pause(self) -> None:
        await self._run_playerctl("play-pause")
        await self.refresh()

    async def next(self) -> None:
        await self._run_playerctl("next")
        await self.refresh()

    async def previous(self) -> None:
        await self._run_playerctl("previous")
        await self.refresh()

    @staticmethod
    def _validated_position(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Position must be a non-negative finite number.")
        try:
            numeric = float(value)
        except (OverflowError, ValueError):
            raise ValueError(
                "Position must be a non-negative finite number."
            ) from None
        if not math.isfinite(numeric) or numeric < 0:
            raise ValueError("Position must be a non-negative finite number.")
        return numeric

    async def seek(self, position_seconds: float) -> None:
        position = self._validated_position(position_seconds)
        if self._snapshot.duration_seconds > 0:
            position = min(position, self._snapshot.duration_seconds)
        await self._run_playerctl("position", str(round(position, 2)))
        self._position_reference = position
        self._position_updated_at = time.monotonic()

    # ── Synchronous wrappers for synchronous IPC handlers ──────────────────

    def play_sync(self) -> dict:
        with self._control_lock:
            self._run_playerctl_sync("play")
        self.refresh_sync()
        return self.get_status_dict()

    def pause_sync(self) -> dict:
        with self._control_lock:
            self._run_playerctl_sync("pause")
        self.refresh_sync()
        return self.get_status_dict()

    def play_pause_sync(self) -> dict:
        with self._control_lock:
            self._run_playerctl_sync("play-pause")
        self.refresh_sync()
        return self.get_status_dict()

    def next_sync(self) -> dict:
        with self._control_lock:
            code, _ = self._run_playerctl_sync("next")
        return {"success": code == 0}

    def previous_sync(self) -> dict:
        with self._control_lock:
            code, _ = self._run_playerctl_sync("previous")
        return {"success": code == 0}

    def seek_sync(self, position_seconds: float) -> dict:
        position = self._validated_position(position_seconds)
        if self._snapshot.duration_seconds > 0:
            position = min(position, self._snapshot.duration_seconds)
        with self._control_lock:
            self._run_playerctl_sync("position", str(round(position, 2)))
        self._position_reference = position
        self._position_updated_at = time.monotonic()
        return {"position_seconds": round(position, 2)}

    # ── Background monitor lifecycle ───────────────────────────────────────

    def start(self) -> None:
        """Start the dedicated monitor thread.

        KIVO's backend is synchronous, so relying on a pre-existing asyncio
        loop would silently disable polling, lyrics and artwork in production.
        """
        if self._running or self._closed:
            return
        self._running = True
        self._loop_ready.clear()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="kivo-media",
            daemon=True,
        )
        self._thread.start()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._loop_ready.set()
        try:
            self._monitor_task = loop.create_task(self._monitor_loop())
            loop.run_forever()
        except Exception:
            pass
        finally:
            self._monitor_task = None
            try:
                pending = [
                    task
                    for task in asyncio.all_tasks(loop)
                    if not task.done()
                ]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.run_until_complete(self._close_async_clients())
            except Exception:
                pass
            finally:
                self._loop = None
                try:
                    loop.close()
                except Exception:
                    pass

    async def _monitor_loop(self) -> None:
        while self._running and not self._closed:
            try:
                await self.refresh()
            except Exception:
                pass
            try:
                await asyncio.sleep(
                    1.0 if self._snapshot.status == "Playing" else 3.0
                )
            except asyncio.CancelledError:
                return

    async def _shutdown_async(self) -> None:
        self._running = False
        tasks = [self._monitor_task, *self._bg_tasks]
        current = asyncio.current_task()
        active = [
            task
            for task in tasks
            if task is not None and task is not current and not task.done()
        ]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self._monitor_task = None
        self._lyrics_task = None
        self._art_task = None
        await self._close_async_clients()

    async def _close_async_clients(self) -> None:
        if self._clients_closed:
            return
        self._clients_closed = True
        if self._owns_lyrics:
            try:
                await self._lyrics_client.close()
            except Exception:
                pass
        if self._owns_artwork:
            try:
                await self._artwork.close()
            except Exception:
                pass

    def close_sync(self) -> None:
        """Stop the monitor thread and release HTTP clients."""
        self._closed = True
        self._running = False
        thread = self._thread
        loop = self._loop

        if thread is not None and thread.is_alive():
            self._loop_ready.wait(timeout=1.0)
            loop = self._loop
            if loop is not None and loop.is_running():
                if threading.current_thread() is thread:
                    loop.call_soon_threadsafe(self._cancel_all_on_loop)
                else:
                    try:
                        future = asyncio.run_coroutine_threadsafe(
                            self._shutdown_async(), loop
                        )
                        future.result(timeout=3.0)
                    except Exception:
                        pass
                    try:
                        loop.call_soon_threadsafe(loop.stop)
                    except Exception:
                        pass
                    thread.join(timeout=2.0)
            elif threading.current_thread() is not thread:
                thread.join(timeout=1.0)
            self._thread = None
        else:
            self._close_clients_without_thread()

    def _cancel_all_on_loop(self) -> None:
        self._running = False
        tasks = [self._monitor_task, *self._bg_tasks]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()

    def _close_clients_without_thread(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(self._close_async_clients())
            except Exception:
                pass
            return
        # Called from a live loop that is not the media loop; close in a
        # short-lived helper thread so we never nest event loops.
        helper = threading.Thread(
            target=self._run_close_clients_thread,
            name="kivo-media-close",
            daemon=True,
        )
        helper.start()
        helper.join(timeout=2.0)

    def _run_close_clients_thread(self) -> None:
        try:
            asyncio.run(self._close_async_clients())
        except Exception:
            pass

    async def close(self) -> None:
        """Stop monitoring and release resources from an async caller."""
        self._closed = True
        self._running = False
        thread = self._thread
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(self.close_sync)
            return
        await self._shutdown_async()


__all__ = ["MediaService", "MediaSnapshot"]
