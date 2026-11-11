# Stage 12.1.2 — Media Security Policy

This document records the security decisions made for the MPRIS/artwork
hardening pass. It is normative for the media subsystem until a later stage
explicitly replaces it.

## 1. Metadata framing

`playerctl` free-text metadata is no longer concatenated with a delimiter.
`MediaService` lists players with `playerctl -l` and reads each of
`xesam:artist`, `xesam:title`, `xesam:album` and `mpris:artUrl` in a separate
argument-vector call (`playerctl -p <player> metadata <key>`).

Only the status enum, position and length are read together with the internal
frame `status|position|length`. Those three fields are constrained to a known
enum and numeric microseconds, so a `|` in a title or artist cannot shift
fields. Embedded newlines and Unicode remain inside their own field.

Player names are validated against a restricted character set and are always
passed as argv elements, never through a shell. Every call has a timeout,
bounded stdout/stderr and explicit process termination.

## 2. Approved local artwork directory

Local artwork is accepted only below:

```
$XDG_CACHE_HOME/kivo/artwork      (default: ~/.cache/kivo/artwork)
```

The directory is created with mode `0700`; cached files are written with mode
`0600`. The directory itself must not be a symlink. Local paths are resolved
with symlinks followed, then checked with `is_relative_to()`; traversal,
symlink escapes, non-regular files, unreadable files and files larger than
16 MiB are rejected.

Consequence: player-provided artwork in `/tmp` (for example Chromium's
temporary files) or in another application's cache (for example VLC's
`~/.cache/vlc/art`) is rejected and the QML fallback glyph is shown. This is
intentional. KIVO does not read arbitrary paths just to render cover art.

## 3. Accepted artwork schemes

| Form | Decision |
|---|---|
| `https://` | Accepted after URL validation and public-address DNS checks, then downloaded by the backend. |
| `http://` | Disabled by default. `ArtworkResolver(allow_http=True)` exists only for a documented future compatibility requirement; no current KIVO configuration enables it. |
| `file://` / absolute path | Accepted only below the approved directory. `file://host/...` is rejected. |
| `qrc:/kivo/artwork/...` | Accepted form; KIVO does not currently emit it. |
| `image://kivo-artwork/<id>` | Accepted form; KIVO does not currently emit it. |
| `javascript:`, `data:`, `exec:`, unknown schemes | Rejected. |
| Malformed URLs, credentials, non-default ports, control characters, overlong values | Rejected. |

## 4. Remote retrieval controls

Remote artwork is fetched by the Python backend, never by QML:

* connection timeout: 2 s; read/write/pool timeout: 3 s;
* maximum response body: 2 MiB (declared length and streamed bytes);
* `Accept-Encoding: identity`, no proxies from the environment (`trust_env=False`);
* redirects are followed manually, at most three, and every destination is
  re-parsed and re-resolved before the next request;
* only `https` destinations are allowed by default; redirects to blocked
  addresses or `http` fail closed;
* content type must be an image type (or `application/octet-stream`, still
  subject to image validation);
* Pillow header validation accepts only PNG, JPEG, WebP and GIF, with
  dimensions at most 4096 × 4096 and at most 16 MP;
* successful bytes are written atomically below the approved directory and
  served to QML as a `file://` URI. Raw image bytes never cross IPC;
* the URL result cache is in-memory and bounded; on-disk artwork is pruned to
  16 files / 32 MiB; failures are negatively cached for 60 s to prevent a
  once-per-poll retry storm.

DNS is checked immediately before each request, and every address returned for
a hostname must be public. DNS rebinding between the check and the HTTP client's
own resolution remains a residual TOCTOU risk; see “Known limitations”.

## 5. Privacy

* Lyrics requests send only artist, track, album (when present) and duration to
  `https://lrclib.net/api/get`. They are automatic when a new track has a
  non-empty artist and title.
* Artwork requests send the artwork URL's host and path, a KIVO user agent and
  the client IP to that host. No referrer, cookies or KIVO metadata are sent
  beyond the URL itself.
* No microphone audio, PCM buffers, screenshots or raw image bytes are read or
  transmitted by the media subsystem.
* QML only accepts `file://` below the approved directory plus the two
  documented internal provider forms.

## 6. Known limitations

* DNS rebinding (TOCTOU) is not fully eliminated without pinning the resolved
  address in the TLS transport. Public-only resolution and revalidation on
  every redirect reduce, but do not remove, this risk.
* Remote artwork URLs may contain tracking identifiers chosen by the player.
  Fetching them is an explicit network disclosure to that host.
* Local artwork outside the approved directory is rejected; playback, lyrics
  and controls continue with the fallback artwork.
* Image validation checks the container header and dimensions, not every
  pixel. QML decodes at 48 × 48 via `sourceSize`; only the four common image
  formats are accepted.
* The cache directory is user-owned; a user who deliberately places files
  there has opted into showing them.
