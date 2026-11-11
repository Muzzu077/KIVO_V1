# KIVO — Stage 13.4 Desktop UI Runtime Audit

```
Document:   Stage 13.4.0 Desktop UI Runtime Audit
Status:     Audit complete — specification input
Date:       2026-11-11
Baseline:   HEAD 1062c0d (working tree contains prior uncommitted Stage 13 work)
Scope:      Actual QML shell, existing KivoIpc transport, and the authoritative
            Clipboard, File Shelf, and Phone Bridge backend/IPC contracts
Mode:       Read-only audit. No Python, QML, IPC, protocol, or clipboard-DB change.
```

---

## 1. Executive Summary

KIVO already has working backend subsystems for clipboard history, a reference-based File
Shelf, and the Phone Bridge. None of them has a dashboard page or any `KivoIpc` method,
property, signal, or event handler. `shell/services/KivoIpc.qml` contains zero occurrences of
`clip_`, `shelf_`, or `bridge_`, confirming that the UI has no wiring for these features.

The backend contracts are usable for a Stage 13.4 UI with three exceptions that constrain the
design:

1. **File Shelf `shelf_open_item` still has a pathname TOCTOU race.** It validates identity
   through a descriptor, closes the descriptor, and then launches `xdg-open` with the
   pathname. The `Open` action must not be exposed until the descriptor handoff described in
   `STAGE_13_4_UI_SECURITY_REVIEW.md` is implemented.
2. **The download QR handoff payload is built but not exposed over IPC.** The shell can read
   `download_id`, `device_id`, `filename`, `size`, and `sha256` from `public_download()`, but
   the canonical `kivo-download://v1?...` payload is not returned by any operation.
3. **"Empty" and "service unavailable" are indistinguishable for clipboard and shelf.**
   When the backend service is absent, both list handlers return a successful empty list.

Two additional constraints are verified but non-blocking:

- The shell is dark-only. `Theme.qml` has no light palette and no theme switch.
- The dashboard is fixed at 880 × 620 with a 200 px navigation rail and a 680 × 540 page host.
  There are no responsive breakpoints. New pages must behave correctly inside that fixed host,
  including long device/file names, large item counts, empty states, and the 680 px content
  width.

The correct Stage 13.4.0 verdict is **READY WITH LIMITATIONS**: the specification can be
implemented as staged work, but `Open` stays hidden and download-QR delivery needs an additive
contract decision before Stage 13.4.2 can be called complete.

---

## 2. Evidence Base

### 2.1 Files inspected

| Area | Files |
|---|---|
| Shell entry | `shell/shell.qml` |
| IPC client | `shell/services/KivoIpc.qml` |
| Dashboard | `shell/components/KivoDashboard.qml` and all `Dashboard*.qml` pages |
| Notch | `shell/components/DynamicNotch.qml`, `NotchMediaDrawer.qml`, `TelemetrySummary.qml`, `MicControlRow.qml` |
| Theme | `shell/styles/Theme.qml` |
| Clipboard backend | `kivo/clipboard/models.py`, `service.py`, `pin_store.py`, `runner.py`, `privacy.py` |
| Shelf backend | `kivo/shelf/models.py`, `service.py`, `path_policy.py` |
| Bridge backend | `kivo/bridge/models.py`, `service.py`, `pairing.py`, `transfer.py`, `transfer_validation.py`, `download.py`, `download_validation.py`, `download_handoff.py`, `server.py`, `quarantine.py`, `qr.py` |
| IPC contract | `kivo/ipc/handlers.py`, `kivo/ipc/protocol.py` |
| Lifecycle/events | `kivo/app.py` |
| Tests | `tests/test_clipboard_*`, `tests/test_shelf_*`, `tests/test_bridge_*`, `shell/tests/test_notch.py` |

### 2.2 Runtime inventory — verified

| Component | Verified state |
|---|---|
| Backend process | Python 3.14, `python -m kivo`, components started in `kivo/app.py` |
| IPC socket | `$XDG_RUNTIME_DIR/kivo/kivo.sock`, newline JSON v1, 64 KB max frame |
| Sole IPC client | One `KivoIpc` scope created in `shell/shell.qml`; the notch and dashboard share it |
| Notch panel | `PanelWindow` at top-centre, implicit 600 × 380, transparent, input mask = capsule |
| Dashboard panel | `PanelWindow`, implicit 880 × 620, independent visibility |
| Dashboard pages | `overview`, `conversation`, `audio`, `activity`, `agents`, `desktop`, `settings` |
| Dashboard nav | 200 px fixed left rail, 44 px items, 4 px spacers |
| Page host | 680 × 540 fixed; each page fills it |
| Clipboard IPC | 6 operations, 1 count-only event |
| Shelf IPC | 5 operations, 1 count-only event |
| Bridge IPC | 10 operations, 6 events |
| Bridge QR | Pairing QR only, delivered as `data:image/png` in status |

### 2.3 Confirmed absence

- No `DashboardClipboard`, `DashboardFileShelf`, or `DashboardPhoneBridge` file exists.
- No `clip_*`, `shelf_*`, or `bridge_*` token appears in `KivoIpc.qml`.
- No dashboard nav entry points to clipboard, shelf, or bridge.
- No `KivoIpc` property exposes bridge status, pairing SAS, transfers, or downloads.
- No QML component renders a QR image.
- No QML component renders a remote device name or remote filename today.

---

## 3. Shell Runtime Contract

### 3.1 Panel geometry and focus

`shell/shell.qml` creates two layer-shell surfaces:

| Surface | Width | Height | Focus | Layer | Input |
|---|---:|---:|---|---|---|
| Notch | implicit 600 | implicit 380 | `OnDemand` only while expanded and `KIVO_NOTCH_FOCUS_ON_EXPAND=1` | `top` by default; `overlay` via `KIVO_NOTCH_LAYER` | `Region { item: notch.inputRegion }` |
| Dashboard | implicit 880 | implicit 620 | `true` window; keyboard focus only while `dashboardOpen` | `top` | `Region { item: dash }` |

The notch never reserves desktop space (`exclusiveZone: 0`, `ExclusionMode.Ignore`). The
dashboard is independent: opening it does not expand or collapse the notch.

### 3.2 Theme tokens — verified values

| Group | Tokens |
|---|---|
| Surfaces | `surfaceTop #1A201B`, `surfaceBottom #101411`, `surfaceRaised #1E251F`, `surfaceSunken #0C100D`, `surfaceHover #202922` |
| Lines | `border #2C372F`, `borderStrong #3D4C41`, `divider #243028` |
| Text | `textPrimary #E8EFE9`, `textSecondary #9BA89F`, `textMuted #6F7C73`, `textFaint #526057` |
| Accents | `accent #6FBF8E`, `accentStrong #8FE0B0`, `accentSoft #4E8B68`, `accentDim #31543F`, `mint #9AD9B4`, `warning #D6B36A`, `danger #D98A7A`, `dangerDeep #8C4C42`, `neutral #A9B3AC`, `offline #6F7A73` |
| Type | `fontFamily "Noto Sans"`, `monoFamily "JetBrainsMono Nerd Font"`; sizes 9, 10, 11, 13, 17, 21 |
| Geometry | collapsed 40, expanded width 520, radius 20/26, top margin 8 |
| Motion | 80 / 140 / 260 / 420 ms |

There is **no** `Theme.light*` token, no palette selection, and no persisted appearance
preference. Settings describes reduced motion as an environment setting only. Stage 13.4 must
not claim light mode support; the security/architecture documents must label light tokens as
proposed future work.

### 3.3 KivoIpc transport behavior — verified

| Behavior | Implementation |
|---|---|
| Request IDs | `kivo-<n>`, callbacks stored in `_pending` |
| Disconnected request | Immediate local error `{code: "not_connected"}` |
| Reconnect | On `_onConnected`, resets `lastSequence`, `sequenceGaps`, `staleEvents`, telemetry/mic revisions; requests status/task/telemetry/mic, then subscribes |
| Disconnect | `connected=false`, `subscribed=false`, `stale=true`, fails pending callbacks with `connection_lost` |
| Event validation | Integer sequence ≥ 1 required |
| Duplicate/out-of-order | `sequence <= lastSequence` increments `staleEvents` and is dropped |
| Gap | `sequence > lastSequence + 1` increments `sequenceGaps` and warns; event is still applied |
| Unknown event | Warns and ignores |
| Response shape | Requires `ok === true` and an object `result`; otherwise invokes callback with a structured error |
| Snapshot apply | `_applyStatus` requires `service === "kivo"`, `protocol_version === 1`, string assistant/audio/agent states |

Stage 13.4 must add clipboard/shelf/bridge properties and handlers without changing these
transport rules, and must resynchronise on every `connectionChanged`/reconnect.

### 3.4 Existing dashboard layout

- Header height 56, body = 564 px.
- Nav rail 200 px wide, page host starts at x=200 with width 680 and height 540.
- Pages fill the host; existing pages use 10 px gaps, 12 px card radii, 14 px inner text
  insets, `Theme.fontBody` for primary row text, `Theme.fontCaption` for labels.
- Nav items are hand-written repeated `Item` blocks. Adding pages requires three new nav
  blocks plus three page instances.
- No page uses a responsive breakpoint. Narrow-width correctness must come from `elide`,
  `maximumLineCount`, `wrapMode`, and minimum widths.

### 3.5 Existing notch layout — verified

| Region | Content | Height behavior |
|---|---|---|
| Capsule | Gradient surface, border, shadow layers | `expandedHeight` is computed, not content-driven |
| Collapsed row | Character icon, "KIVO", waveform/dots, label, elapsed, stop, CPU chip, mic chip, status dot | Fixed 40 px |
| Expanded header | Icon, title, state pill, pin | 34 px |
| Divider | 1 px | 10 px below header |
| Body | Title/detail, waveform, task progress, media drawer, telemetry, mic row, PTT, transcript, context | Column with 8 px spacing |
| Footer | Backend metadata + hint | 14 px, anchored bottom |

`expandedHeight` base is 212 for idle, 222–252 for result/audio states, 302 for driving/stopping,
plus 134 when idle/offline/syncing and no media, or 78/200 for media. Any new notch row must
add its exact height plus the 8 px column spacing to this computation or it will overlap the
footer.

Media, telemetry, mic, and PTT are already mutually exclusive or stacked in the body. The
notch integration specification therefore limits new content to one compact row and transient
banners.

---

## 4. Clipboard Contract

### 4.1 Operations

| Operation | Params | Success result | Notes |
|---|---|---|---|
| `clip_get_history` | `limit` 1–100, `search` ≤128 chars, `filter` `all|text|image|pinned` | `{items: ClipItem[], total_count: int}` | Returns `{items: [], total_count: 0}` when service is absent |
| `clip_get_item` | `id` 1–64 chars | `{ok: bool, item: ClipDetail|null}` | Masked text if sensitive |
| `clip_copy_item` | `id` | `{ok: bool}` | Copies raw bytes, including sensitive content |
| `clip_pin_item` | `id`, `pin` bool | `{ok: bool}` or `{ok:false, error}` | Sensitive item raises `ValueError` |
| `clip_delete_item` | `id` | `{ok: bool}` | Deletes cliphist row and pin |
| `clip_clear_history` | none | `{ok: bool}` | `cliphist wipe` |

### 4.2 `ClipItem` fields

```text
id, raw_id, type, preview, char_count, size_bytes, timestamp,
is_pinned, is_sensitive, privacy_level, mime_type
```

`type` is `text` or `image`; `privacy_level` is `normal` or `sensitive`. `raw_id` is cliphist's
identifier and must never be shown to the user.

`ClipDetail` adds:

```text
text_content, has_binary_data
```

For sensitive items, `text_content` is always `"••••••••••••"`, even when the backend decoded
the content internally.

### 4.3 Event

```json
{ "event": "clip_history_changed", "data": { "count": 12 } }
```

Only the count is emitted. The UI must refresh through `clip_get_history`; it must not assume
the list changed in any particular way.

### 4.4 Privacy behavior — verified

| Property | Verified behavior |
|---|---|
| Sensitive preview | Masked in `ClipItem.to_dict()` via the privacy filter |
| Sensitive detail | Masked in `ClipDetail.to_dict()` |
| Sensitive pin | Rejected by `ClipboardService.pin_item` with `ValueError` |
| Sensitive copy | **Allowed** by `clip_copy_item`; the backend decodes and writes the raw bytes |
| History event secrecy | Count only; no previews or text |
| Pin storage | `$XDG_DATA_HOME/kivo/clipboard_pins.json`, mode 0600, metadata only |
| Pin sensitivity marker | Pin file never marks an entry sensitive; that state comes from the live filter |
| Clear history | `cliphist wipe` deletes **all** raw history |
| Pinned items after wipe | Pin metadata survives and pinned entries remain listed; their raw cliphist content is gone, so copy/decode fails |
| Upstream persistence | `wl-paste --watch cliphist store` writes content before KIVO can filter it |

The docstring on `clear_history` says "unpinned", but the implementation calls `cliphist wipe`
and removes pinned raw content too. The UI copy must describe the actual behavior and warn
that pinned entries will become unreadable.

### 4.5 Missing contract for UI

| Gap | Consequence |
|---|---|
| No `available`/`error_code` in history response | A missing clipboard service looks like an empty history |
| No event payload items | Every change requires a list refresh |
| No bulk delete | Not required for Stage 13.4 |
| No automatic AI access | Verified: no clipboard tool exists in the agent/tool packages |

---

## 5. File Shelf Contract

### 5.1 Operations

| Operation | Params | Success result | Notes |
|---|---|---|---|
| `shelf_list_items` | none | `{items: ShelfItem[]}` | Performs freshness checks and may emit a change event |
| `shelf_add_uris` | `uris` 1–20 strings, each ≤4096 bytes | `{added: ShelfItem[], rejected: [{uri, code, message}]}` | Duplicate URI returns the existing item in `added` |
| `shelf_remove_item` | `id` | `{ok: bool}` | Disk file untouched |
| `shelf_clear` | none | `{ok: bool}` | Disk files untouched |
| `shelf_open_item` | `id` | `{ok, error_code, error_message}` | **TOCTOU: pathname-based launch** |

### 5.2 `ShelfItem` public fields

```text
id, name, file_type, size_bytes, staged_at, stale, availability
```

Withheld internal fields: `_canonical_path`, `_identity` (dev, ino, size, mtime_ns, mode,
mnt_id), `_is_workspace`, `_workspace_name`.

- `id` matches `shelf_[0-9a-f]{12}`.
- `name` is sanitized: bidi overrides and control characters replaced, basename only, ≤128 chars.
- `file_type` is a lower-case extension or MIME subtype, or `unknown`.
- `stale`/`availability` become `true`/`unavailable` when identity verification fails.

### 5.3 Event

```json
{ "event": "shelf_items_changed", "data": { "count": 3 } }
```

Count only. The UI refreshes via `shelf_list_items`.

### 5.4 Verified TOCTOU

`shelf/service.py`:

1. `verify_existing_identity(item._canonical_path, item._identity)` opens/validates and closes
   its descriptor.
2. `run_bounded([xdg_open, item._canonical_path], timeout=2.0)` launches by pathname.

Between step 1 and step 2 the path can be replaced. Identity checks at one instant do not
authorize a later pathname resolution. `authorize_export()` is descriptor-based and
revalidates identity, but it is a different method and does not protect `open_item()`.

**Stage 13.4 decision:** the dashboard must render `Open` as disabled/unavailable with the
reason "Open is unavailable until descriptor-safe launching is implemented". Only `Remove`
and export/download authorization are exposed in Stage 13.4.1/13.4.2.

### 5.5 Missing contract for UI

| Gap | Consequence |
|---|---|
| No `available`/`error_code` in list response | A missing shelf service looks like an empty shelf |
| No picker/portal add flow | The UI cannot add files without an external URI source; Stage 13.4.1 needs a file-dialog or drag/drop decision |
| No descriptor-safe open | `Open` blocked |
| No export authorization operation | Downloads use `bridge_create_download_ticket` |

The shell itself has no file-picker component. A native `QtQuick.Dialogs`/portal file dialog is
required for "Add files", or the page must accept only URIs supplied by an existing desktop
mechanism. Stage 13.4.1 must decide this explicitly; it is not implemented today.

---

## 6. Phone Bridge Contract

### 6.1 Status operation

`bridge_get_status` returns `BridgeStatus.to_dict()`:

```text
state, lan_ip, port, cert_fingerprint, qr_data_url,
pending_pairing, active_device, error_message
```

State values: `idle`, `offering`, `confirming`, `paired`, `rejected`, `revoked`, `expired`,
`error`.

`pending_pairing`:

```text
session_id, client_id, device_name, client_ip, sas_code, expires_at
```

`active_device`:

```text
session_id, client_id, device_name, client_ip,
created_at, last_active, expires_at, is_revoked
```

`qr_data_url` is a `data:image/png;base64,...` rendering of a `kivo-pair://v1?...` URI that
contains the pairing secret. It is present only while an offer exists and disappears after
the handshake consumes the offer.

### 6.2 Operations

| Operation | Params | Result |
|---|---|---|
| `bridge_start` | `port` 1024–65535 | `{ok, status?}` or `{ok:false, error}` |
| `bridge_stop` | none | `{ok: bool}` |
| `bridge_get_status` | none | `BridgeStatus` |
| `bridge_approve_pairing` | `session_id` (or legacy `device_id`) | `{ok: bool}` |
| `bridge_reject_pairing` | `session_id` | `{ok: bool}` |
| `bridge_revoke_device` | `session_id` | `{ok: bool}` |
| `bridge_get_transfers` | none | `{ok, transfers: PublicTransfer[]}` |
| `bridge_resolve_transfer` | `transfer_id`, `decision` in `allow_receive|reject_receive|accept_file|discard_file` | `{ok, transfer}` or structured error |
| `bridge_cancel_transfer` | `transfer_id` `tr_<24 hex>` | `{ok, transfer}` |
| `bridge_create_download_ticket` | `item_id` `shelf_<12 hex>`, `authorize` must be literal `true` | `{ok, download}` or structured error |
| `bridge_get_downloads` | none | `{ok, downloads: PublicDownload[]}` |
| `bridge_cancel_download` | `download_id` `tkt_<64 hex>` | `{ok, download}` |

### 6.3 Upload states

```text
created
awaiting_desktop_approval   ← Gate A pending
receiving
verifying
awaiting_final_acceptance   ← Gate B pending
committed
rejected
cancelled
expired
failed
discarded
```

Verified rules:

- Metadata creation does not reserve storage and does not start receiving.
- `allow_receive` is Gate A; it validates state and reserves quarantine capacity.
- Chunks require a valid paired session and the authorized transfer.
- `verifying` begins after all declared bytes arrive.
- `accept_file` is Gate B; it publishes a verified file into Downloads.
- `discard_file` is a separate Gate B decision.
- Rejection at Gate A consumes no quarantine capacity.
- Approval TTL 60 s; stalled timeout 30 s; final acceptance TTL 900 s; terminal retention 300 s.
- Maximum upload 2 GiB; chunk 1 MiB; max concurrent 2; max active per session 2.

### 6.4 Download states

```text
issued → claimed → streaming → completed
                             ↘ expired | revoked | failed | cancelled
```

Verified rules:

- Creation requires a paired session, a current shelf item, `authorize === true`, and a
  descriptor-based export grant whose identity is revalidated.
- Ticket TTL 5 min; active lease 30 min; idle timeout 30 s.
- Ticket is bound to one session, one device, and one file identity.
- Chunks require claim first and are read from the retained descriptor.
- Cancellation, revocation, expiry, and completion are terminal.
- `public_download()` never includes `item_id`, paths, descriptors, inodes, session IDs, or
  tokens.

### 6.5 Public transfer fields

```text
transfer_id, filename, size, received_bytes, progress_percent, state,
device_name, mime, sha256, error_code, published_filename,
chunk_size, next_chunk_index, created_at, updated_at, expires_at
```

No bearer tokens, session keys, quarantine paths, reservations, or hasher state.

### 6.6 Public download fields

```text
download_id, device_id, filename, file_type, size, sha256,
bytes_sent, progress_percent, state, device_name, chunk_size,
next_chunk_index, total_chunks, created_at, updated_at,
expires_at, claim_expires_at, active_expires_at, error_code
```

No `item_id`. After reconnect, a download cannot be reliably re-associated with its shelf item
from the public list alone.

### 6.7 Events

| Event | Payload |
|---|---|
| `bridge_status_changed` | `BridgeStatus` |
| `bridge_pairing_requested` | Subset: `session_id`, `client_id`, `device_name`, `client_ip`, `sas_code`; it has no `expires_at`. The service also publishes `bridge_status_changed` with the authoritative `pending_pairing`, which supplies the expiry |
| `bridge_transfer_requested` | `PublicTransfer` |
| `bridge_transfer_progress` | `PublicTransfer` |
| `bridge_transfer_completed` | `PublicTransfer` |
| `bridge_download_requested` | `PublicDownload` |
| `bridge_download_progress` | `PublicDownload` |
| `bridge_download_completed` | `PublicDownload` |

These are emitted by `kivo/app.py` only if the event name is in the allowed set. All payloads
are public/path-free.

### 6.8 Authorization chain — verified

| Step | Enforcement |
|---|---|
| Bridge start | Physical LAN interface required; ephemeral ECDSA P-256 cert; port allocation |
| Pairing offer | 5-minute TTL; 32-byte pairing secret; server nonce; QR `kivo-pair://v1` |
| Client handshake | HMAC proof over canonical context; per-IP and global failed-proof limits; lockout |
| SAS | Derived code displayed to both sides; desktop sees `pending_pairing.sas_code` |
| Desktop approval | 60-second window; `bridge_approve_pairing` requires matching session ID |
| Rejection | `bridge_reject_pairing` clears pending request |
| Session | 2-hour TTL; bearer token and upstream/downstream keys never in IPC |
| Revocation | `bridge_revoke_device` revokes session and cancels its transfers/downloads |
| Expiry | Offer, approval, session, transfer approval, final acceptance, ticket, lease all expire |
| Upload Gate A | `allow_receive` only from `awaiting_desktop_approval` |
| Upload Gate B | `accept_file` only from `awaiting_final_acceptance` |
| Download | Explicit `authorize: true`; claim before chunks; bound ticket |

### 6.9 Missing contract for UI

| Gap | Consequence |
|---|---|
| No handoff payload in IPC | Cannot render the canonical download QR without duplicating the encoder |
| No `item_id` in public download | Reconnect loses shelf association |
| No pairing-offer `expires_at` in status | Status has `qr_data_url` but no offer expiry timestamp; UI can only use a status poll |
| No status event at lazy expiry | Offer/session/approval expiry is noticed on the next status read; the UI must poll |
| `get_status` has no `available` flag | Missing bridge service returns a default idle status, indistinguishable from a real idle bridge |
| Pairing QR is only in status | QR disappears after handshake; UI must not cache or log it |

---

## 7. State-Truth Rules for the Future UI

| Rule | Reason |
|---|---|
| Never show optimistic success | Backend events are the only terminal truth |
| Refresh on count-only events | Event data contains no item list |
| Resync all sections after reconnect | Sequence resets and in-memory backend state may differ |
| Treat gap as possible missed change | Events may be filtered per client; a gap does not prove loss, but refresh is cheap |
| Ignore duplicate/out-of-order events | Existing `KivoIpc` already drops them |
| Disable mutations while disconnected/stale | No request can be confirmed |
| Clear approval cards on `idle`/`revoked`/`expired` | Pending state is ephemeral and non-recoverable |
| Keep QR/SAS out of logs and snapshots | Secret-bearing values |
| Never bind `Open` until descriptor-safe | TOCTOU |

---

## 8. Limitations Register

| ID | Limitation | Severity | Blocks |
|---|---|---|---|
| L1 | Shelf `open_item` pathname TOCTOU | High | Exposing `Open` |
| L2 | Download handoff not exposed over IPC | Medium | Canonical download QR |
| L3 | Empty vs unavailable indistinguishable | Medium | Accurate unavailable state |
| L4 | `get_downloads` omits `item_id` | Low | Shelf association after reconnect |
| L5 | Dark-only theme; no light tokens | Low | Light-mode acceptance |
| L6 | Fixed 880×620 dashboard; no responsive breakpoints | Low | Narrow-window claims |
| L7 | No file picker in the shell | Medium | Adding files from the dashboard |
| L8 | `clear_history` wipes pinned raw content | Medium | Clear confirmation copy |
| L9 | Upstream cliphist persists before filtering | High but upstream | Zero-trace clipboard claim |
| L10 | No physical Android device/emulator available | High for end-to-end | Physical pairing/transfer validation |

---

## 9. Audit Verdict

**READY WITH LIMITATIONS.**

Verified behavior is sufficient to specify and build the three dashboards and the restrained
notch integration. The specification must keep `Open` unavailable, treat download-QR delivery
as an additive contract prerequisite, and state honestly that empty/unavailable cannot be
distinguished until the list responses carry availability metadata.

Proposed design is separated from verified behavior in
`STAGE_13_4_DESKTOP_UI_ARCHITECTURE.md` and `STAGE_13_4_UI_SECURITY_REVIEW.md`. No file in this
stage was modified other than the four Stage 13.4 documents.
