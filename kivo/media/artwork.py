"""Artwork URL policy and bounded remote artwork resolver.

Policy summary
--------------
* ``https://`` is the only remote scheme.  ``http://`` is disabled unless a
  caller explicitly opts in for a documented compatibility case.
* Remote destinations must be public: loopback, private, link-local,
  multicast, reserved and unspecified addresses are rejected.  Hostnames are
  resolved and every returned address is checked.
* ``file://`` and absolute paths are accepted only below the approved artwork
  cache directory (``$XDG_CACHE_HOME/kivo/artwork`` by default).  Traversal,
  symlink escapes, non-regular files and control characters are rejected.
* ``qrc:/kivo/artwork/`` and ``image://kivo-artwork/`` are the only accepted
  internal forms.  KIVO does not currently emit either form.
* Remote artwork is downloaded by the backend with timeouts, a hard byte cap,
  manual redirect validation, and Pillow header/dimension checks.  The bytes
  never cross IPC; QML receives a ``file://`` URI below the approved cache.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import os
import re
import socket
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx
from PIL import Image, UnidentifiedImageError

MAX_ARTWORK_URL_LENGTH = 4096
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
MAX_LOCAL_ARTWORK_BYTES = 16 * 1024 * 1024
MAX_IMAGE_DIMENSION = 4096
MAX_IMAGE_PIXELS = 16_000_000
MAX_REDIRECTS = 3
MAX_CACHE_ENTRIES = 16
MAX_CACHE_BYTES = 32 * 1024 * 1024
NEGATIVE_CACHE_SECONDS = 60.0

QRC_ARTWORK_PREFIX = "qrc:/kivo/artwork/"
IMAGE_ARTWORK_PREFIX = "image://kivo-artwork/"

_SAFE_IMAGE_FORMATS = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "WEBP": ".webp",
    "GIF": ".gif",
}
_ALLOWED_CONTENT_TYPES = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "application/octet-stream",
)
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".home.arpa",
    ".lan",
    ".test",
    ".invalid",
    ".example",
    ".onion",
)
_CACHE_FILE_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|webp|gif)$")


@dataclass(frozen=True)
class ArtworkCandidate:
    """A validated artwork reference.

    ``display_url`` is safe to hand to QML.  ``remote_url`` is an untrusted
    public HTTPS URL that still needs an asynchronous, bounded download.
    """

    display_url: str = ""
    remote_url: str = ""


def _is_public_ip(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(value, ipaddress.IPv6Address) and value.ipv4_mapped is not None:
        value = value.ipv4_mapped
    return bool(
        value.is_global
        and not value.is_private
        and not value.is_loopback
        and not value.is_link_local
        and not value.is_multicast
        and not value.is_reserved
        and not value.is_unspecified
    )


def _contains_control(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


class ArtworkResolver:
    """Validate artwork references and fetch remote artwork under policy."""

    def __init__(
        self,
        approved_root: Optional[Path] = None,
        allow_http: bool = False,
        timeout_seconds: float = 3.0,
        connect_timeout_seconds: float = 2.0,
        max_download_bytes: int = MAX_DOWNLOAD_BYTES,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self._allow_http = bool(allow_http)
        self._timeout_seconds = max(0.5, float(timeout_seconds))
        self._connect_timeout = max(0.5, float(connect_timeout_seconds))
        self._max_download_bytes = max(1024, int(max_download_bytes))

        self._root = self._resolve_root(approved_root)

        timeout = httpx.Timeout(
            self._timeout_seconds, connect=self._connect_timeout
        )
        self._http = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

        self._url_cache: OrderedDict[str, str] = OrderedDict()
        self._failed: OrderedDict[str, float] = OrderedDict()
        self._closed = False

    # ── Approved local directory ───────────────────────────────────────────

    @staticmethod
    def default_root() -> Path:
        configured = os.environ.get("XDG_CACHE_HOME", "").strip()
        if configured and os.path.isabs(configured):
            base = Path(configured)
        else:
            base = Path.home() / ".cache"
        return base / "kivo" / "artwork"

    def _resolve_root(self, approved_root: Optional[Path]) -> Path:
        self._root_usable = False
        root = Path(approved_root) if approved_root is not None else self.default_root()
        try:
            root = root.expanduser()
            if not root.is_absolute():
                root = Path.home() / ".cache" / "kivo" / "artwork"
            if root.is_symlink():
                # An attacker-controlled symlink at the approved location must
                # not turn /etc (or any other directory) into an artwork root.
                return root.absolute()
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if root.is_symlink():
                return root.absolute()
            os.chmod(root, 0o700)
            resolved = root.resolve(strict=True)
            if not resolved.is_dir():
                return root.absolute()
            self._root_usable = True
            return resolved
        except Exception:
            # Keep a deterministic path; validation and persistence fail
            # closed because _root_usable remains False.
            return root.absolute()

    @property
    def approved_root(self) -> Path:
        return self._root

    # ── Synchronous preparation ────────────────────────────────────────────

    def prepare(self, raw_url: str) -> ArtworkCandidate:
        """Validate a metadata artwork value without network or blocking I/O."""
        if not isinstance(raw_url, str):
            return ArtworkCandidate()
        raw = raw_url.strip()
        if not raw or len(raw) > MAX_ARTWORK_URL_LENGTH or _contains_control(raw):
            return ArtworkCandidate()

        lowered = raw.lower()
        if lowered.startswith("//"):
            return ArtworkCandidate()
        if lowered.startswith("file:"):
            local = self._validate_local_url(raw)
            return ArtworkCandidate(display_url=local) if local else ArtworkCandidate()
        if lowered.startswith("qrc:") or lowered.startswith("image:"):
            internal = self._validate_internal_url(raw)
            return ArtworkCandidate(display_url=internal) if internal else ArtworkCandidate()
        if lowered.startswith("https:") or lowered.startswith("http:"):
            normalized = self._parse_remote_url(raw)
            if normalized is None:
                return ArtworkCandidate()
            cached = self._cached_local(normalized)
            if cached:
                return ArtworkCandidate(display_url=cached)
            if self._is_negative_cached(normalized):
                return ArtworkCandidate()
            return ArtworkCandidate(remote_url=normalized)
        if raw.startswith("/"):
            local = self._validate_local_path(raw)
            return ArtworkCandidate(display_url=local) if local else ArtworkCandidate()
        return ArtworkCandidate()

    def _validate_local_url(self, raw: str) -> str:
        try:
            parts = urlsplit(raw)
        except ValueError:
            return ""
        if parts.scheme.lower() != "file" or parts.netloc:
            return ""
        if parts.query or parts.fragment:
            return ""
        return self._validate_local_path(unquote(parts.path))

    def _validate_local_path(self, raw_path: str) -> str:
        if not self._root_usable or not raw_path or _contains_control(raw_path):
            return ""
        try:
            path = Path(raw_path)
            if not path.is_absolute():
                return ""
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(self._root):
                return ""
            if resolved == self._root:
                return ""
            info = resolved.stat()
            if not resolved.is_file() or info.st_size > MAX_LOCAL_ARTWORK_BYTES:
                return ""
            if not os.access(resolved, os.R_OK):
                return ""
            return resolved.as_uri()
        except (OSError, RuntimeError, ValueError):
            return ""

    def _validate_internal_url(self, raw: str) -> str:
        if _contains_control(raw) or "\\" in raw:
            return ""
        lowered = raw.lower()
        if lowered.startswith(QRC_ARTWORK_PREFIX):
            remainder = raw[len(QRC_ARTWORK_PREFIX):]
            if not remainder or len(remainder) > 512:
                return ""
            if ".." in remainder or remainder.startswith("/"):
                return ""
            if not re.fullmatch(r"[A-Za-z0-9._/-]+", remainder):
                return ""
            return raw
        if lowered.startswith(IMAGE_ARTWORK_PREFIX):
            remainder = raw[len(IMAGE_ARTWORK_PREFIX):]
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", remainder):
                return ""
            return raw
        return ""

    # ── Remote URL policy ──────────────────────────────────────────────────

    def _parse_remote_url(self, raw: str) -> Optional[str]:
        if _contains_control(raw) or "\\" in raw:
            return None
        try:
            parts = urlsplit(raw)
        except ValueError:
            return None
        scheme = parts.scheme.lower()
        if scheme not in ("https", "http"):
            return None
        if scheme == "http" and not self._allow_http:
            return None
        if parts.username or parts.password:
            return None
        host = parts.hostname
        if not host:
            return None
        try:
            port = parts.port
        except ValueError:
            return None
        expected_port = 443 if scheme == "https" else 80
        if port not in (None, expected_port):
            return None

        host = host.rstrip(".").lower()
        if not host:
            return None
        if host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
            return None

        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Hostnames must be qualified and IDNA-encodable.  DNS is checked
            # immediately before the request, where blocking is acceptable.
            if "." not in host or len(host) > 253:
                return None
            try:
                host = host.encode("idna").decode("ascii")
            except UnicodeError:
                return None
            netloc = host
        else:
            if not _is_public_ip(address):
                return None
            netloc = (
                f"[{address.compressed}]"
                if isinstance(address, ipaddress.IPv6Address)
                else address.compressed
            )

        if port is not None:
            netloc = f"{netloc}:{port}"
        path = parts.path or "/"
        query = parts.query
        return urlunsplit((scheme, netloc, path, query, ""))

    def _resolve_public_host(self, host: str, port: int) -> bool:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError, ValueError):
            return False
        if not infos:
            return False
        for info in infos:
            sockaddr = info[4]
            if not sockaddr:
                return False
            address_text = str(sockaddr[0]).split("%", 1)[0]
            try:
                address = ipaddress.ip_address(address_text)
            except ValueError:
                return False
            if not _is_public_ip(address):
                return False
        return True

    async def _validate_remote_destination(self, url: str) -> bool:
        parts = urlsplit(url)
        host = parts.hostname
        if not host:
            return False
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return await asyncio.to_thread(self._resolve_public_host, host, port)

    # ── Bounded remote download ────────────────────────────────────────────

    async def fetch_remote(self, raw_url: str) -> Optional[str]:
        """Download and validate remote artwork, returning a local file URI.

        Any failure returns ``None`` and never raises into the media service.
        """
        if self._closed:
            return None
        if not self._root_usable:
            return None
        normalized = self._parse_remote_url(raw_url)
        if normalized is None:
            return None
        cached = self._cached_local(normalized)
        if cached:
            return cached
        if self._is_negative_cached(normalized):
            return None

        current = normalized
        try:
            for _ in range(MAX_REDIRECTS + 1):
                if not await self._validate_remote_destination(current):
                    self._record_failure(normalized)
                    return None
                async with self._http.stream(
                    "GET",
                    current,
                    headers={
                        "Accept": "image/png,image/jpeg,image/webp,image/gif",
                        "Accept-Encoding": "identity",
                        "User-Agent": "KIVO artwork resolver",
                    },
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location", "")
                        if not location:
                            self._record_failure(normalized)
                            return None
                        redirected = urljoin(current, location)
                        next_url = self._parse_remote_url(redirected)
                        if next_url is None:
                            self._record_failure(normalized)
                            return None
                        current = next_url
                        continue

                    if response.status_code != 200:
                        self._record_failure(normalized)
                        return None

                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if content_type and content_type not in _ALLOWED_CONTENT_TYPES:
                        self._record_failure(normalized)
                        return None

                    declared = response.headers.get("content-length")
                    if declared:
                        try:
                            if int(declared) > self._max_download_bytes:
                                self._record_failure(normalized)
                                return None
                        except ValueError:
                            self._record_failure(normalized)
                            return None

                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > self._max_download_bytes:
                            self._record_failure(normalized)
                            return None
                        data.extend(chunk)
                    if not data:
                        self._record_failure(normalized)
                        return None

                    local = self._store_validated_image(bytes(data), normalized)
                    if local is None:
                        self._record_failure(normalized)
                    return local
        except Exception:
            self._record_failure(normalized)
            return None
        self._record_failure(normalized)
        return None

    def _store_validated_image(self, data: bytes, source_url: str) -> Optional[str]:
        if not self._root_usable:
            return None
        try:
            with Image.open(io.BytesIO(data)) as image:
                image_format = image.format
                width, height = image.size
                if image_format not in _SAFE_IMAGE_FORMATS:
                    return None
                if width <= 0 or height <= 0:
                    return None
                if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
                    return None
                if width * height > MAX_IMAGE_PIXELS:
                    return None
                image.verify()
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
            return None
        except Exception:
            return None

        suffix = _SAFE_IMAGE_FORMATS[image_format]
        digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:32]
        target = self._root / f"{digest}{suffix}"
        temp_path = ""
        try:
            self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(dir=self._root, prefix=".download-")
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, target)
        except OSError:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            return None

        self._url_cache[source_url] = target.as_uri()
        while len(self._url_cache) > 64:
            self._url_cache.popitem(last=False)
        self._prune_cache()
        return target.as_uri()

    def _prune_cache(self) -> None:
        try:
            files = [
                path
                for path in self._root.iterdir()
                if path.is_file() and _CACHE_FILE_RE.match(path.name)
            ]
            files.sort(key=lambda item: item.stat().st_mtime)
            total = sum(item.stat().st_size for item in files)
            while files and (
                len(files) > MAX_CACHE_ENTRIES or total > MAX_CACHE_BYTES
            ):
                oldest = files.pop(0)
                total -= oldest.stat().st_size
                try:
                    oldest.unlink()
                except OSError:
                    pass
            live = {path.as_uri() for path in files}
            for key in list(self._url_cache):
                if self._url_cache[key] not in live:
                    self._url_cache.pop(key, None)
        except OSError:
            pass

    def _cached_local(self, url: str) -> str:
        if not self._root_usable:
            return ""
        uri = self._url_cache.get(url)
        if not uri:
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
            for suffix in _SAFE_IMAGE_FORMATS.values():
                candidate = self._root / f"{digest}{suffix}"
                try:
                    if candidate.is_file() and candidate.stat().st_size > 0:
                        uri = candidate.as_uri()
                        self._url_cache[url] = uri
                        break
                except OSError:
                    continue
            while len(self._url_cache) > 64:
                self._url_cache.popitem(last=False)
        if not uri:
            return ""
        try:
            path = Path(unquote(urlsplit(uri).path))
            if path.is_file() and path.resolve(strict=True).is_relative_to(self._root):
                return uri
        except (OSError, RuntimeError, ValueError):
            pass
        self._url_cache.pop(url, None)
        return ""

    def _is_negative_cached(self, url: str) -> bool:
        timestamp = self._failed.get(url)
        if timestamp is None:
            return False
        if time.monotonic() - timestamp > NEGATIVE_CACHE_SECONDS:
            self._failed.pop(url, None)
            return False
        return True

    def _record_failure(self, url: str) -> None:
        self._failed[url] = time.monotonic()
        while len(self._failed) > 32:
            self._failed.popitem(last=False)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._http.aclose()
        except Exception:
            pass


__all__ = [
    "ArtworkCandidate",
    "ArtworkResolver",
    "IMAGE_ARTWORK_PREFIX",
    "MAX_DOWNLOAD_BYTES",
    "MAX_IMAGE_DIMENSION",
    "MAX_IMAGE_PIXELS",
    "QRC_ARTWORK_PREFIX",
]
