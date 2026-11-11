"""Unit and adversarial tests for MPRIS metadata, artwork policy and lyrics."""
from __future__ import annotations

import asyncio
import base64
import io
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from PIL import Image

from kivo.media.artwork import ArtworkResolver
from kivo.media.lyrics import LyricsClient, LyricLine, LyricsResult
from kivo.media.service import MediaService, MediaSnapshot

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class LyricsClientAdversarialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = LyricsClient(timeout_seconds=1.0)

    async def asyncTearDown(self) -> None:
        await self.client.close()

    def test_parse_lrc_lines_sorted_and_normalized(self):
        lrc_text = (
            "[00:32.40] Look how they shine for you\n"
            "[00:28.00] Look at the stars\n"
            "[01:05.50] Yeah they were all yellow\n"
        )
        lines = self.client.parse_lrc(lrc_text)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0].time_seconds, 28.0)
        self.assertEqual(lines[0].text, "Look at the stars")
        self.assertEqual(lines[1].time_seconds, 32.4)
        self.assertEqual(lines[1].text, "Look how they shine for you")
        self.assertEqual(lines[2].time_seconds, 65.5)

    def test_multiple_timestamps_on_one_line(self):
        lines = self.client.parse_lrc("[00:12.00][00:24.50] Repeated chorus line")
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].time_seconds, 12.0)
        self.assertEqual(lines[1].time_seconds, 24.5)
        self.assertEqual(lines[0].text, "Repeated chorus line")
        self.assertEqual(lines[1].text, "Repeated chorus line")

    def test_alternate_timestamp_precisions(self):
        lrc_text = (
            "[00:05] Integer seconds\n"
            "[00:10.5] Single decimal\n"
            "[00:15.25] Two decimals\n"
            "[00:20.125] Three decimals\n"
        )
        lines = self.client.parse_lrc(lrc_text)
        self.assertEqual([line.time_seconds for line in lines], [5.0, 10.5, 15.25, 20.12])

    def test_ignores_metadata_header_tags_and_malformed_lines(self):
        lrc_text = (
            "[ti:Yellow]\n"
            "[ar:Coldplay]\n"
            "[al:Parachutes]\n"
            "[length:04:27]\n"
            "[by:LyricsCreator]\n"
            "[invalid] ignore this\n"
            "plain text without tag\n"
            "[00:28.00] Look at the stars\n"
        )
        lines = self.client.parse_lrc(lrc_text)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].time_seconds, 28.0)
        self.assertEqual(lines[0].text, "Look at the stars")

    async def test_get_lyrics_success_synced(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "trackName": "Yellow",
            "artistName": "Coldplay",
            "syncedLyrics": "[00:28.00] Look at the stars\n[00:32.40] Look how they shine",
            "plainLyrics": "Look at the stars\nLook how they shine",
        }
        with patch.object(self.client._http, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = response
            result = await self.client.get_lyrics("Coldplay", "Yellow")
        self.assertIsNotNone(result)
        self.assertTrue(result.synced)
        self.assertEqual(len(result.lines), 2)
        self.assertEqual(result.lines[0].time_seconds, 28.0)

    async def test_get_lyrics_plain_unsynchronized_fallback(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "syncedLyrics": None,
            "plainLyrics": "First line\nSecond line",
        }
        with patch.object(self.client._http, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = response
            result = await self.client.get_lyrics("Artist", "Instrumental")
        self.assertIsNotNone(result)
        self.assertFalse(result.synced)
        self.assertEqual([line.text for line in result.lines], ["First line", "Second line"])
        self.assertTrue(all(line.time_seconds == 0.0 for line in result.lines))

    async def test_get_lyrics_not_found_is_cached(self):
        response = MagicMock()
        response.status_code = 404
        with patch.object(self.client._http, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = response
            self.assertIsNone(await self.client.get_lyrics("Unknown Artist", "Unknown Song"))
            self.assertIsNone(await self.client.get_lyrics("Unknown Artist", "Unknown Song"))
            self.assertEqual(mock_get.call_count, 1)

    async def test_network_timeout_returns_none_and_is_not_cached(self):
        with patch.object(self.client._http, "get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = httpx.TimeoutException("Network timeout")
            self.assertIsNone(await self.client.get_lyrics("Coldplay", "Yellow"))
            self.assertNotIn(("coldplay", "yellow"), self.client._cache)

    async def test_rate_limit_429_returns_none_and_is_not_cached(self):
        response = MagicMock()
        response.status_code = 429
        with patch.object(self.client._http, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = response
            self.assertIsNone(await self.client.get_lyrics("Coldplay", "Yellow"))
            self.assertNotIn(("coldplay", "yellow"), self.client._cache)

    async def test_server_error_500_returns_none_and_is_not_cached(self):
        response = MagicMock()
        response.status_code = 500
        with patch.object(self.client._http, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = response
            self.assertIsNone(await self.client.get_lyrics("Coldplay", "Yellow"))
            self.assertNotIn(("coldplay", "yellow"), self.client._cache)

    async def test_malformed_json_returns_none(self):
        response = MagicMock()
        response.status_code = 200
        response.json.side_effect = ValueError("Invalid JSON response")
        with patch.object(self.client._http, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = response
            self.assertIsNone(await self.client.get_lyrics("Coldplay", "Yellow"))

    async def test_empty_artist_or_track_returns_none_immediately(self):
        self.assertIsNone(await self.client.get_lyrics("", "Yellow"))
        self.assertIsNone(await self.client.get_lyrics("Coldplay", ""))
        self.assertIsNone(await self.client.get_lyrics("   ", "   "))

    async def test_lru_cache_eviction(self):
        client = LyricsClient(max_cache_entries=3)
        try:
            for index in range(5):
                response = MagicMock()
                response.status_code = 200
                response.json.return_value = {
                    "trackName": f"Song {index}",
                    "artistName": "Artist",
                    "syncedLyrics": f"[00:10.00] Song {index}",
                }
                with patch.object(client._http, "get", new_callable=AsyncMock) as mock_get:
                    mock_get.return_value = response
                    await client.get_lyrics("Artist", f"Song {index}")
            self.assertEqual(len(client._cache), 3)
            self.assertIn(("artist", "song 4"), client._cache)
            self.assertNotIn(("artist", "song 0"), client._cache)
        finally:
            await client.close()


class ArtworkPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "approved"
        self.root.mkdir()
        self.resolver = ArtworkResolver(approved_root=self.root)
        self.inside = self.root / "cover.png"
        self.inside.write_bytes(_PNG_BYTES)
        self.outside = self.base / "outside.png"
        self.outside.write_bytes(_PNG_BYTES)

    def tearDown(self) -> None:
        asyncio.run(self.resolver.close())
        self.temp.cleanup()

    def test_public_https_is_candidate_for_bounded_fetch(self):
        candidate = self.resolver.prepare("https://cdn.example.com/art.png")
        self.assertEqual(candidate.display_url, "")
        self.assertEqual(candidate.remote_url, "https://cdn.example.com/art.png")

    def test_http_disabled_by_default(self):
        self.assertEqual(self.resolver.prepare("http://example.com/art.png").remote_url, "")

    def test_local_art_inside_approved_directory(self):
        candidate = self.resolver.prepare(self.inside.as_uri())
        self.assertEqual(candidate.display_url, self.inside.resolve().as_uri())
        self.assertEqual(candidate.remote_url, "")

    def test_absolute_local_path_inside_approved_directory(self):
        candidate = self.resolver.prepare(str(self.inside))
        self.assertEqual(candidate.display_url, self.inside.resolve().as_uri())

    def test_local_art_outside_approved_directory_rejected(self):
        self.assertEqual(
            self.resolver.prepare(self.outside.as_uri()).display_url, ""
        )
        self.assertEqual(self.resolver.prepare(str(self.outside)).display_url, "")

    def test_local_traversal_rejected(self):
        traversal = f"file://{self.root}/../outside.png"
        self.assertEqual(self.resolver.prepare(traversal).display_url, "")

    def test_symlink_escape_rejected(self):
        link = self.root / "escape.png"
        os.symlink(self.outside, link)
        self.assertEqual(self.resolver.prepare(link.as_uri()).display_url, "")

    def test_symlinked_approved_root_rejected(self):
        real = self.base / "real-root"
        real.mkdir()
        linked = self.base / "linked-root"
        os.symlink(real, linked)
        resolver = ArtworkResolver(approved_root=linked)
        try:
            target = real / "cover.png"
            target.write_bytes(_PNG_BYTES)
            self.assertEqual(resolver.prepare(target.as_uri()).display_url, "")
            self.assertFalse(resolver._root_usable)
        finally:
            asyncio.run(resolver.close())

    def test_symlink_inside_approved_directory_allowed(self):
        link = self.root / "safe.png"
        os.symlink(self.inside, link)
        self.assertEqual(
            self.resolver.prepare(link.as_uri()).display_url,
            self.inside.resolve().as_uri(),
        )

    def test_directory_is_not_artwork(self):
        subdirectory = self.root / "folder"
        subdirectory.mkdir()
        self.assertEqual(self.resolver.prepare(subdirectory.as_uri()).display_url, "")

    def test_file_uri_with_host_rejected(self):
        self.assertEqual(
            self.resolver.prepare(f"file://localhost{self.inside}").display_url, ""
        )

    def test_control_characters_rejected(self):
        self.assertEqual(
            self.resolver.prepare(f"{self.inside.as_uri()}\x00").display_url, ""
        )
        self.assertEqual(
            self.resolver.prepare(f"{self.inside.as_uri()}%00").display_url, ""
        )
        self.assertEqual(
            self.resolver.prepare("https://example.com/a\nb.png").remote_url, ""
        )

    def test_unsupported_schemes_rejected(self):
        for url in (
            "javascript:alert(1)",
            "data:image/png;base64,AAAA",
            "exec:reboot",
            "ftp://example.com/a.png",
            "gopher://example.com/a.png",
            "//example.com/a.png",
            "https://",
            "not a url",
        ):
            candidate = self.resolver.prepare(url)
            self.assertEqual(candidate.display_url, "", url)
            self.assertEqual(candidate.remote_url, "", url)

    def test_loopback_private_and_link_local_ips_rejected(self):
        for url in (
            "https://127.0.0.1/a.png",
            "https://10.0.0.1/a.png",
            "https://192.168.1.1/a.png",
            "https://169.254.1.1/a.png",
            "https://[::1]/a.png",
            "https://[fe80::1]/a.png",
            "https://[fd00::1]/a.png",
            "https://0.0.0.0/a.png",
        ):
            self.assertEqual(self.resolver.prepare(url).remote_url, "", url)

    def test_blocked_hostnames_and_ports_rejected(self):
        for url in (
            "https://localhost/a.png",
            "https://foo.local/a.png",
            "https://foo.internal/a.png",
            "https://foo.test/a.png",
            "https://intranet/a.png",
            "https://user:pass@example.com/a.png",
            "https://example.com:8443/a.png",
        ):
            self.assertEqual(self.resolver.prepare(url).remote_url, "", url)

    def test_documented_internal_forms(self):
        self.assertEqual(
            self.resolver.prepare("qrc:/kivo/artwork/cover.png").display_url,
            "qrc:/kivo/artwork/cover.png",
        )
        self.assertEqual(
            self.resolver.prepare("image://kivo-artwork/cover-1").display_url,
            "image://kivo-artwork/cover-1",
        )
        for url in (
            "qrc:/other/cover.png",
            "qrc:/kivo/artwork/../secret.png",
            "image://other-provider/cover",
            "image://kivo-artwork/../cover",
        ):
            self.assertEqual(self.resolver.prepare(url).display_url, "", url)

    def test_overlong_url_rejected(self):
        self.assertEqual(
            self.resolver.prepare("https://example.com/" + "a" * 5000).remote_url,
            "",
        )


def _public_dns(host, port, *args, **kwargs):
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("93.184.216.34", port),
        )
    ]


class _ChunkedStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"x" * 700
        yield b"x" * 700


class ArtworkRemoteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "approved"
        self.root.mkdir()
        self.requests: list[str] = []

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _resolver(self, handler, **kwargs) -> ArtworkResolver:
        transport = httpx.MockTransport(handler)
        return ArtworkResolver(
            approved_root=self.root,
            transport=transport,
            **kwargs,
        )

    @staticmethod
    def _image_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=_PNG_BYTES,
        )

    async def test_public_https_download_is_cached_and_local(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return self._image_handler(request)

        resolver = self._resolver(handler)
        try:
            with patch("kivo.media.artwork.socket.getaddrinfo", _public_dns):
                result = await resolver.fetch_remote("https://cdn.example.com/art.png")
                cached = await resolver.fetch_remote("https://cdn.example.com/art.png")
        finally:
            await resolver.close()

        self.assertIsNotNone(result)
        self.assertEqual(result, cached)
        path = Path(result.removeprefix("file://"))
        self.assertTrue(path.is_file())
        self.assertTrue(path.resolve().is_relative_to(self.root.resolve()))
        self.assertEqual(path.read_bytes(), _PNG_BYTES)
        self.assertEqual(calls, ["https://cdn.example.com/art.png"])

    async def test_downloaded_artwork_survives_process_cache_reset(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return self._image_handler(request)

        resolver = self._resolver(handler)
        try:
            with patch("kivo.media.artwork.socket.getaddrinfo", _public_dns):
                first = await resolver.fetch_remote("https://cdn.example.com/art.png")
                resolver._url_cache.clear()
                second = await resolver.fetch_remote("https://cdn.example.com/art.png")
        finally:
            await resolver.close()
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)

    async def test_http_allowed_only_when_explicitly_opted_in(self):
        resolver = self._resolver(self._image_handler, allow_http=True)
        try:
            with patch("kivo.media.artwork.socket.getaddrinfo", _public_dns):
                result = await resolver.fetch_remote("http://cdn.example.com/art.png")
        finally:
            await resolver.close()
        self.assertIsNotNone(result)

    async def test_default_http_never_requests(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return self._image_handler(request)

        resolver = self._resolver(handler)
        try:
            result = await resolver.fetch_remote("http://cdn.example.com/art.png")
        finally:
            await resolver.close()
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    async def test_unusable_approved_root_blocks_remote_fetch(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return self._image_handler(request)

        real = Path(self.temp.name) / "real-root"
        real.mkdir()
        linked = Path(self.temp.name) / "linked-root"
        os.symlink(real, linked)
        resolver = ArtworkResolver(
            approved_root=linked,
            transport=httpx.MockTransport(handler),
        )
        try:
            with patch("kivo.media.artwork.socket.getaddrinfo", _public_dns):
                result = await resolver.fetch_remote(
                    "https://cdn.example.com/art.png"
                )
        finally:
            await resolver.close()
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    async def test_redirect_to_public_destination_is_revalidated(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            if request.url.host == "cdn.example.com":
                return httpx.Response(
                    302,
                    headers={"location": "https://images.example.org/cover.png"},
                )
            return self._image_handler(request)

        resolver = self._resolver(handler)
        try:
            with patch("kivo.media.artwork.socket.getaddrinfo", _public_dns):
                result = await resolver.fetch_remote("https://cdn.example.com/art.png")
        finally:
            await resolver.close()
        self.assertIsNotNone(result)
        self.assertEqual(len(calls), 2)

    async def test_redirect_to_blocked_destination_rejected(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(
                302,
                headers={"location": "https://127.0.0.1/secret.png"},
            )

        resolver = self._resolver(handler)
        try:
            with patch("kivo.media.artwork.socket.getaddrinfo", _public_dns):
                result = await resolver.fetch_remote("https://cdn.example.com/art.png")
        finally:
            await resolver.close()
        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)

    async def test_redirect_to_http_rejected_when_http_disabled(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(
                302,
                headers={"location": "http://cdn.example.com/plain.png"},
            )

        resolver = self._resolver(handler)
        try:
            with patch("kivo.media.artwork.socket.getaddrinfo", _public_dns):
                result = await resolver.fetch_remote("https://cdn.example.com/art.png")
        finally:
            await resolver.close()
        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)

    async def test_hostname_resolving_to_private_address_rejected(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return self._image_handler(request)

        def private_dns(host, port, *args, **kwargs):
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("10.0.0.7", port),
                )
            ]

        resolver = self._resolver(handler)
        try:
            with patch("kivo.media.artwork.socket.getaddrinfo", private_dns):
                result = await resolver.fetch_remote("https://cdn.example.com/art.png")
        finally:
            await resolver.close()
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    async def test_link_local_resolution_rejected(self):
        resolver = self._resolver(self._image_handler)

        def link_local_dns(host, port, *args, **kwargs):
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("169.254.10.10", port),
                )
            ]

        try:
            with patch("kivo.media.artwork.socket.getaddrinfo", link_local_dns):
                result = await resolver.fetch_remote("https://cdn.example.com/art.png")
        finally:
            await resolver.close()
        self.assertIsNone(result)

    async def test_oversized_content_length_rejected(self):
        def handler(request):
            return httpx.Response(
                200,
                headers={"content-type": "image/png"},
                content=b"x" * 2048,
            )

        resolver = self._resolver(handler, max_download_bytes=1024)
        try:
            with patch("kivo.media.artwork.socket.getaddrinfo", _public_dns):
                result = await resolver.fetch_remote("https://cdn.example.com/art.png")
        finally:
            await resolver.close()
        self.assertIsNone(result)

    async def test_oversized_stream_without_content_length_rejected(self):
        def handler(request):
            return httpx.Response(
                200,
                headers={"content-type": "image/png"},
                stream=_ChunkedStream(),
            )

        resolver = self._resolver(handler, max_download_bytes=1024)
        try:
            with patch("kivo.media.artwork.socket.getaddrinfo", _public_dns):
                result = await resolver.fetch_remote("https://cdn.example.com/art.png")
        finally:
            await resolver.close()
        self.assertIsNone(result)

    async def test_invalid_image_data_rejected(self):
        def handler(request):
            return httpx.Response(
                200,
                headers={"content-type": "image/png"},
                content=b"this is not an image",
            )

        resolver = self._resolver(handler)
        try:
            with patch("kivo.media.artwork.socket.getaddrinfo", _public_dns):
                result = await resolver.fetch_remote("https://cdn.example.com/art.png")
        finally:
            await resolver.close()
        self.assertIsNone(result)

    async def test_unexpected_content_type_rejected_before_decode(self):
        def handler(request):
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"<html></html>",
            )

        resolver = self._resolver(handler)
        try:
            with patch("kivo.media.artwork.socket.getaddrinfo", _public_dns):
                result = await resolver.fetch_remote("https://cdn.example.com/art.png")
        finally:
            await resolver.close()
        self.assertIsNone(result)

    async def test_read_timeout_returns_none(self):
        def handler(request):
            raise httpx.ReadTimeout("timed out", request=request)

        resolver = self._resolver(handler)
        try:
            with patch("kivo.media.artwork.socket.getaddrinfo", _public_dns):
                result = await resolver.fetch_remote("https://cdn.example.com/art.png")
        finally:
            await resolver.close()
        self.assertIsNone(result)

    async def test_oversized_image_dimensions_rejected(self):
        buffer = io.BytesIO()
        Image.new("RGB", (5000, 1), color=(1, 2, 3)).save(buffer, format="PNG")
        oversized = buffer.getvalue()

        def handler(request):
            return httpx.Response(
                200,
                headers={"content-type": "image/png"},
                content=oversized,
            )

        resolver = self._resolver(handler)
        try:
            with patch("kivo.media.artwork.socket.getaddrinfo", _public_dns):
                result = await resolver.fetch_remote("https://cdn.example.com/art.png")
        finally:
            await resolver.close()
        self.assertIsNone(result)

    async def test_negative_cache_avoids_repeated_requests(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(500)

        resolver = self._resolver(handler)
        try:
            with patch("kivo.media.artwork.socket.getaddrinfo", _public_dns):
                first = await resolver.fetch_remote("https://cdn.example.com/art.png")
                second = await resolver.fetch_remote("https://cdn.example.com/art.png")
        finally:
            await resolver.close()
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(calls), 1)


class MediaServiceMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "artwork"
        self.root.mkdir()
        self.resolver = ArtworkResolver(approved_root=self.root)
        self.lyrics = MagicMock()
        self.lyrics.get_lyrics = AsyncMock(return_value=None)
        self.service = MediaService(
            lyrics_client=self.lyrics,
            artwork_resolver=self.resolver,
        )

    async def asyncTearDown(self) -> None:
        await self.service.close()
        await self.resolver.close()
        self.temp.cleanup()

    def _snapshot(self, **overrides) -> MediaSnapshot:
        values = {
            "player_name": "chromium.instance4841",
            "status": "Playing",
            "artist": "Coldplay",
            "title": "Yellow",
            "album": "Parachutes",
            "art_url": "",
            "length_raw": "267000000",
            "position_raw": "35000000",
        }
        values.update(overrides)
        snapshot = self.service._snapshot_from_fields(
            values["player_name"],
            values["status"],
            values["artist"],
            values["title"],
            values["album"],
            values["art_url"],
            values["length_raw"],
            values["position_raw"],
        )
        self.assertIsNotNone(snapshot)
        return snapshot

    def test_delimiter_text_in_title_and_artist_is_preserved(self):
        snapshot = self._snapshot(
            artist="Artist __KIVO_MPRIS_SEP__ Name",
            title="Title __KIVO_MPRIS_SEP__ 🎵",
        )
        self.assertEqual(snapshot.artist, "Artist __KIVO_MPRIS_SEP__ Name")
        self.assertEqual(snapshot.title, "Title __KIVO_MPRIS_SEP__ 🎵")

    def test_numeric_frame_delimiter_in_text_is_field_local(self):
        snapshot = self._snapshot(
            artist="A|B|C",
            title="Title | with | pipes",
            album="Album | Deluxe",
        )
        self.assertEqual(snapshot.artist, "A|B|C")
        self.assertEqual(snapshot.title, "Title | with | pipes")
        self.assertEqual(snapshot.album, "Album | Deluxe")
        self.assertEqual(snapshot.status, "Playing")

    def test_embedded_newlines_are_preserved_and_do_not_shift_fields(self):
        snapshot = self._snapshot(
            artist="Artist line 1\nArtist line 2",
            title="Title line 1\nTitle line 2",
            album="Album line 1\nAlbum line 2",
        )
        self.assertEqual(snapshot.artist, "Artist line 1\nArtist line 2")
        self.assertEqual(snapshot.title, "Title line 1\nTitle line 2")
        self.assertEqual(snapshot.album, "Album line 1\nAlbum line 2")

    def test_unicode_and_emoji_preserved(self):
        snapshot = self._snapshot(
            artist="坂本龍一 🌏",
            title="Merry Christmas Mr. Lawrence — 戦場のメリークリスマス",
            album="1999 ✨",
        )
        self.assertEqual(snapshot.artist, "坂本龍一 🌏")
        self.assertEqual(snapshot.title, "Merry Christmas Mr. Lawrence — 戦場のメリークリスマス")
        self.assertEqual(snapshot.album, "1999 ✨")

    def test_empty_optional_metadata(self):
        snapshot = self._snapshot(artist="", album="", length_raw="", position_raw="")
        self.assertEqual(snapshot.artist, "")
        self.assertEqual(snapshot.album, "")
        self.assertEqual(snapshot.duration_seconds, 0.0)
        self.assertEqual(snapshot.position_seconds, 0.0)

    def test_malformed_numeric_metadata_fails_safe(self):
        snapshot = self._snapshot(length_raw="nan", position_raw="inf")
        self.assertEqual(snapshot.duration_seconds, 0.0)
        self.assertEqual(snapshot.position_seconds, 0.0)
        snapshot = self._snapshot(length_raw="not-a-number", position_raw="-5")
        self.assertEqual(snapshot.duration_seconds, 0.0)
        self.assertEqual(snapshot.position_seconds, 0.0)

    def test_malformed_status_rejected(self):
        for status in ("", "Bogus", "playing\ninjected"):
            self.assertIsNone(
                self.service._snapshot_from_fields(
                    "player", status, "artist", "title", "", "", "", ""
                )
            )

    def test_invalid_artwork_is_dropped(self):
        snapshot = self._snapshot(art_url="javascript:alert(1)")
        self.assertEqual(snapshot.art_url, "")
        self.assertEqual(snapshot.pending_art_url, "")

    def test_remote_artwork_is_not_exposed_to_qml(self):
        snapshot = self._snapshot(art_url="https://cdn.example.com/art.png")
        self.assertEqual(snapshot.art_url, "")
        self.assertEqual(snapshot.pending_art_url, "https://cdn.example.com/art.png")

    async def test_multi_player_discovery_prefers_playing_player(self):
        numeric = {
            "vlc": "Paused|1000000|2000000",
            "spotify": "Playing|5000000|267000000",
        }
        text = {
            "vlc": {"xesam:artist": "Old Artist", "xesam:title": "Old Track"},
            "spotify": {
                "xesam:artist": "Coldplay",
                "xesam:title": "Yellow __KIVO_MPRIS_SEP__ 🎵",
                "xesam:album": "Parachutes",
                "mpris:artUrl": "",
            },
        }

        async def fake_run(*args):
            if args == ("-l",):
                return 0, "vlc\nspotify"
            player = args[1]
            if args[2] == "metadata" and args[3] == "--format":
                return 0, numeric[player]
            return 0, text[player].get(args[3], "")

        with patch.object(self.service, "_run_playerctl", side_effect=fake_run):
            snapshot = await self.service.refresh()

        self.assertTrue(snapshot.available)
        self.assertEqual(snapshot.player_name, "spotify")
        self.assertEqual(snapshot.players, ["vlc", "spotify"])
        self.assertEqual(snapshot.title, "Yellow __KIVO_MPRIS_SEP__ 🎵")
        self.assertEqual(snapshot.artist, "Coldplay")
        self.assertEqual(snapshot.duration_seconds, 267.0)
        self.assertEqual(snapshot.position_seconds, 5.0)

    async def test_missing_optional_field_does_not_shift_other_fields(self):
        async def fake_run(*args):
            if args == ("-l",):
                return 0, "player"
            if args[2] == "metadata" and args[3] == "--format":
                return 0, "Playing|1000000|60000000"
            if args[3] == "xesam:artist":
                return 0, "Artist"
            if args[3] == "xesam:title":
                return 0, "Title"
            return 1, ""  # album and art missing

        with patch.object(self.service, "_run_playerctl", side_effect=fake_run):
            snapshot = await self.service.refresh()

        self.assertEqual(snapshot.artist, "Artist")
        self.assertEqual(snapshot.title, "Title")
        self.assertEqual(snapshot.album, "")
        self.assertEqual(snapshot.art_url, "")

    async def test_player_exits_between_discovery_and_read(self):
        async def fake_run(*args):
            if args == ("-l",):
                return 0, "gone\nlive"
            if args[1] == "gone":
                return 1, ""
            if args[2] == "metadata" and args[3] == "--format":
                return 0, "Playing|0|1000000"
            return 0, "Artist" if args[3] == "xesam:artist" else "Title"

        with patch.object(self.service, "_run_playerctl", side_effect=fake_run):
            snapshot = await self.service.refresh()

        self.assertTrue(snapshot.available)
        self.assertEqual(snapshot.player_name, "live")
        self.assertEqual(snapshot.players, ["gone", "live"])

    async def test_all_players_disappear(self):
        async def fake_run(*args):
            if args == ("-l",):
                return 0, "gone"
            return 1, ""

        with patch.object(self.service, "_run_playerctl", side_effect=fake_run):
            snapshot = await self.service.refresh()
        self.assertFalse(snapshot.available)
        self.assertEqual(snapshot.status, "Unavailable")

    async def test_malformed_player_output_fails_safe(self):
        async def fake_run(*args):
            if args == ("-l",):
                return 0, "player"
            if args[2] == "metadata" and args[3] == "--format":
                return 0, "Playing|1"  # missing field
            return 0, ""

        with patch.object(self.service, "_run_playerctl", side_effect=fake_run):
            snapshot = await self.service.refresh()
        self.assertFalse(snapshot.available)

    async def test_remote_artwork_is_resolved_asynchronously(self):
        local = (self.root / "cached.png").as_uri()
        with patch.object(
            self.resolver, "fetch_remote", new=AsyncMock(return_value=local)
        ) as fetch:
            snapshot = self._snapshot(art_url="https://cdn.example.com/art.png")
            self.service._update_snapshot(snapshot)
            await asyncio.sleep(0.05)
            self.assertEqual(self.service.snapshot.art_url, local)
            fetch.assert_awaited_once_with("https://cdn.example.com/art.png")

    async def test_stale_artwork_response_is_discarded(self):
        self.service._snapshot = self._snapshot(art_url="")
        self.service._snapshot.pending_art_url = "https://cdn.example.com/new.png"
        self.service._art_request_id = 2
        with patch.object(
            self.resolver,
            "fetch_remote",
            new=AsyncMock(return_value=(self.root / "old.png").as_uri()),
        ):
            await self.service._fetch_artwork_async(
                "https://cdn.example.com/old.png", 1
            )
        self.assertEqual(self.service.snapshot.art_url, "")

    async def test_delayed_lyrics_response_does_not_overwrite_new_track(self):
        release = asyncio.Event()

        async def delayed_lyrics(artist, track):
            if track == "Track A":
                await release.wait()
                return LyricsResult(
                    track="Track A",
                    artist="Artist A",
                    synced=True,
                    lines=[LyricLine(10.0, "Old lyrics")],
                )
            return LyricsResult(
                track="Track B",
                artist="Artist B",
                synced=True,
                lines=[LyricLine(5.0, "New lyrics")],
            )

        self.lyrics.get_lyrics.side_effect = delayed_lyrics
        snapshot_a = self._snapshot(artist="Artist A", title="Track A")
        snapshot_b = self._snapshot(artist="Artist B", title="Track B")
        self.service._update_snapshot(snapshot_a)
        await asyncio.sleep(0)
        self.service._update_snapshot(snapshot_b)
        release.set()
        await asyncio.sleep(0.05)

        self.assertIsNotNone(self.service.last_lyrics)
        self.assertEqual(self.service.last_lyrics.track, "Track B")

    async def test_track_change_clears_previous_lyrics_immediately(self):
        events: list[LyricsResult] = []
        self.service._on_lyrics_loaded = events.append
        self.lyrics.get_lyrics.side_effect = None
        self.lyrics.get_lyrics.return_value = LyricsResult(
            track="Old", artist="Artist", synced=False, lines=[]
        )
        self.service._update_snapshot(self._snapshot(artist="Artist", title="Old"))
        await asyncio.sleep(0.02)
        self.service._update_snapshot(self._snapshot(artist="Artist", title="New"))
        self.assertTrue(events)
        self.assertEqual(events[-1].track, "New")
        self.assertEqual(events[-1].lines, [])

    async def test_seek_validation_and_clamping(self):
        self.service._snapshot.duration_seconds = 100.0
        for value in (-1.0, float("nan"), float("inf"), True, 10**1000):
            with self.assertRaises(ValueError):
                await self.service.seek(value)
            with self.assertRaises(ValueError):
                self.service.seek_sync(value)

        with patch.object(
            self.service, "_run_playerctl_sync", return_value=(0, "")
        ) as run:
            result = self.service.seek_sync(500.0)
        self.assertEqual(result["position_seconds"], 100.0)
        run.assert_called_once_with("position", "100.0")

    async def test_bounded_process_timeout_kills_child(self):
        started = time.monotonic()
        code, output = self.service._run_bounded_process(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout=0.15,
            output_limit=1024,
        )
        self.assertEqual(code, 1)
        self.assertIn("timed out", output)
        self.assertLess(time.monotonic() - started, 2.0)

    async def test_bounded_process_output_limit(self):
        code, output = self.service._run_bounded_process(
            [sys.executable, "-c", "print('x' * 5000)"],
            timeout=5.0,
            output_limit=1024,
        )
        self.assertEqual(code, 1)
        self.assertIn("too large", output)

    async def test_background_thread_starts_and_stops(self):
        with patch.object(
            self.service, "_run_playerctl_sync", return_value=(1, "No players found")
        ):
            self.service.start()
            self.assertIsNotNone(self.service._thread)
            self.assertTrue(self.service._thread.is_alive())
            await asyncio.sleep(0.1)
            thread = self.service._thread
            self.service.close_sync()
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())

    async def test_no_callbacks_after_close(self):
        events: list[MediaSnapshot] = []
        self.service._on_status_changed = events.append
        self.service._closed = True
        self.service._emit_status()
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
