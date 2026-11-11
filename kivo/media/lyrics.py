"""Asynchronous LRCLIB client and synchronized lyric parser."""
from __future__ import annotations

import collections
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

LRCLIB_API_URL = "https://lrclib.net/api/get"
DEFAULT_LYRICS_TIMEOUT_SECONDS = 3.0
MAX_LYRICS_CACHE_ENTRIES = 32

_TIMESTAMP_TAG = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
_METADATA_TAG = re.compile(r"^\[[a-zA-Z]+:.*\]$")


@dataclass(frozen=True)
class LyricLine:
    """A single timestamped lyric line."""

    time_seconds: float
    text: str


@dataclass(frozen=True)
class LyricsResult:
    """Complete lyric result for one track."""

    track: str
    artist: str
    synced: bool
    lines: list[LyricLine]
    raw_lrc: str = ""

    def to_dict(self) -> dict:
        return {
            "track": self.track,
            "artist": self.artist,
            "synced": self.synced,
            "lines": [
                {"time_seconds": line.time_seconds, "text": line.text}
                for line in self.lines
            ],
        }


class LyricsClient:
    """Queries LRCLIB for synchronized song lyrics with bounded in-memory caching."""

    def __init__(
        self,
        timeout_seconds: float = DEFAULT_LYRICS_TIMEOUT_SECONDS,
        max_cache_entries: int = MAX_LYRICS_CACHE_ENTRIES,
    ):
        self._timeout_seconds = max(0.5, float(timeout_seconds))
        self._http = httpx.AsyncClient(timeout=self._timeout_seconds)
        self._cache: collections.OrderedDict[tuple[str, str], Optional[LyricsResult]] = (
            collections.OrderedDict()
        )
        self._max_cache = max(1, int(max_cache_entries))

    @staticmethod
    def parse_lrc(lrc_text: str) -> list[LyricLine]:
        """Parse raw LRC text into sorted LyricLines, supporting multiple tags per line."""
        lines: list[LyricLine] = []
        if not isinstance(lrc_text, str):
            return lines

        for raw_line in lrc_text.splitlines():
            clean = raw_line.strip()
            if not clean or _METADATA_TAG.match(clean):
                continue
            matches = list(_TIMESTAMP_TAG.finditer(clean))
            if not matches:
                continue

            # Strip all timestamp tags to recover the shared lyric text
            text_part = _TIMESTAMP_TAG.sub("", clean).strip()

            for match in matches:
                minutes_str, seconds_str = match.groups()
                try:
                    minutes = int(minutes_str)
                    seconds = float(seconds_str)
                    time_sec = round(minutes * 60.0 + seconds, 2)
                except (ValueError, TypeError):
                    continue
                lines.append(LyricLine(time_seconds=time_sec, text=text_part))

        lines.sort(key=lambda item: item.time_seconds)
        return lines

    async def get_lyrics(
        self,
        artist: str,
        track: str,
        album: str = "",
        duration_seconds: float = 0.0,
    ) -> Optional[LyricsResult]:
        """Fetch lyrics from LRCLIB or return cached result.

        Transient errors (timeouts, 429, 5xx) fail closed without polluting
        the permanent cache.
        """
        clean_artist = (artist or "").strip()
        clean_track = (track or "").strip()
        if not clean_artist or not clean_track:
            return None

        cache_key = (clean_artist.lower(), clean_track.lower())
        if cache_key in self._cache:
            return self._cache[cache_key]

        params: dict[str, str | float] = {
            "artist_name": clean_artist,
            "track_name": clean_track,
        }
        if album.strip():
            params["album_name"] = album.strip()
        if duration_seconds > 0:
            params["duration"] = round(duration_seconds, 1)

        result: Optional[LyricsResult] = None
        should_cache = False
        try:
            resp = await self._http.get(LRCLIB_API_URL, params=params)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    data = {}
                synced_lrc = data.get("syncedLyrics")
                plain_lrc = data.get("plainLyrics")
                if isinstance(synced_lrc, str) and synced_lrc.strip():
                    lines = self.parse_lrc(synced_lrc)
                    result = LyricsResult(
                        track=clean_track,
                        artist=clean_artist,
                        synced=True,
                        lines=lines,
                        raw_lrc=synced_lrc,
                    )
                elif isinstance(plain_lrc, str) and plain_lrc.strip():
                    lines = [
                        LyricLine(time_seconds=0.0, text=line.strip())
                        for line in plain_lrc.splitlines()
                        if line.strip()
                    ]
                    result = LyricsResult(
                        track=clean_track,
                        artist=clean_artist,
                        synced=False,
                        lines=lines,
                        raw_lrc=plain_lrc,
                    )
                should_cache = True
            elif resp.status_code == 404:
                # Definitively not found in database; safe to cache negative result
                result = None
                should_cache = True
            else:
                # 429 rate limit or 5xx server error; transient, do not cache
                result = None
                should_cache = False
        except Exception:
            result = None
            should_cache = False

        if should_cache:
            self._cache[cache_key] = result
            if len(self._cache) > self._max_cache:
                self._cache.popitem(last=False)
        return result

    async def close(self) -> None:
        """Release underlying HTTP client connections."""
        try:
            await self._http.aclose()
        except Exception:
            pass


__all__ = ["LyricLine", "LyricsClient", "LyricsResult"]
