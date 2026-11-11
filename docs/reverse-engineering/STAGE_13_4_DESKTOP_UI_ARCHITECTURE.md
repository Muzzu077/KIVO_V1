# KIVO — Stage 13.4 Desktop UI Architecture

```
Document:   Stage 13.4 Desktop UI Architecture Specification
Status:     Proposed design — not implemented
Date:       2026-11-11
Baseline:   HEAD 1062c0d; verified contracts in STAGE_13_4_DESKTOP_UI_RUNTIME_AUDIT.md
Scope:      DashboardClipboard, DashboardFileShelf, DashboardPhoneBridge, shared KivoIpc
            state, reusable components, responsive rules, and restrained notch integration
```

This document is a design specification. Everything marked **[VERIFIED]** is grounded in the
audit. Everything marked **[PROPOSED]** is intended work for Stages 13.4.1–13.4.4 and does not
exist yet. No implementation is included in Stage 13.4.0.

---

## 1. Design Principles

1. **One IPC client.** All three pages and the notch use the single `KivoIpc` scope created in
   `shell/shell.qml`. No page opens a socket, starts a process, or constructs backend state.
2. **Backend-confirmed truth only.** A spinner is allowed while a request is in flight. Success,
   failure, completion, cancellation, revocation, and acceptance are shown only after a
   response or event from the backend.
3. **Opaque identifiers only.** The UI addresses `ClipItem.id`, `ShelfItem.id`,
   `UploadTransfer.transfer_id`, `DownloadTicket.download_id`, and `session_id`. It never uses
   `raw_id`, `_canonical_path`, inode/device fields, `item_id` internals, bearer tokens, or
   session keys as display or key values.
4. **Live and stale are visually distinct.** When the socket is down or a resync is pending,
   every page shows a stale banner and disables mutations. Old values remain visible but are
   labelled as last known.
5. **No generic approval.** Pairing, Gate A, and Gate B use different headings, different
   explanations, different button labels, and different confirmation sheets. There is no
   reusable "Approve" button.
6. **No invented availability.** "Empty" is shown only after a successful list response.
   "Unavailable" is shown when the operation fails or when a future availability flag says so.
7. **Restraint in the notch.** Clipboard and shelf have no persistent notch surface. The notch
   gains at most one 28 px productivity row and short backend-confirmed transients.
8. **Preserve the current visual system.** Use `Theme` tokens, dark-only surfaces, 12 px card
   radii, 10 px gaps, and the existing type scale. No new visual language.

---

## 2. Page and Navigation Architecture

### 2.1 New dashboard pages

| Page ID | File | Nav label | Purpose |
|---|---|---|---|
| `clipboard` | `shell/components/DashboardClipboard.qml` | Clipboard | History search/filter, explicit copy, pin/unpin, delete, clear |
| `shelf` | `shell/components/DashboardFileShelf.qml` | File Shelf | Staged references, add/remove/clear, export initiation, Open (disabled until remediation) |
| `bridge` | `shell/components/DashboardPhoneBridge.qml` | Phone Bridge | Pairing, device/session, uploads Gate A/B, downloads and QR |

### 2.2 Navigation insertion

Existing order is preserved. Insert the three pages after `Desktop Context` and before
`Settings`:

```text
Overview → Conversation → Audio → Activity → Agent Workspace → Desktop Context
         → Clipboard → File Shelf → Phone Bridge → Settings
```

Ten items × 44 px + nine 4 px spacers = 476 px, which fits the 564 px body. The 200 px rail and
680 × 540 page host remain unchanged. `KivoDashboard` gains three nav blocks and three page
instances with `visible: dash.currentPage === "<id>"`.

**[PROPOSED]** `KivoDashboard` also exposes:

```qml
function openPage(page) { dash.selectPage(page); }
signal pageSelected(string page)   // already exists
```

`shell.qml` gains `function openDashboardPage(page)` so the notch can request the bridge page
without knowing dashboard internals.

### 2.3 Page frame

All three pages fill the 680 × 540 host. They use:

- 0 px outer margin (matching Overview/Settings), 14 px inner card inset.
- 10 px vertical gaps; 8 px gaps inside cards.
- A 28 px page status line at the top: section title (`Theme.fontTitle`), stale/error badge on
  the right.
- A scrollable body below. Clipboard and shelf use `ListView`; bridge uses a `Flickable`
  containing bounded `Repeater` sections (backend registry caps at 32 transfers and 32
  downloads).
- Footer note only where needed (cliphist persistence, Gate explanations).

---

## 3. Shared State Model in `KivoIpc`

### 3.1 Design rules

- Every section keeps its own phase, data, last-known timestamp, error, and in-flight marker.
- State is replaced as a whole object and then assigned, so QML bindings update reliably.
- Section generations prevent a slow response from overwriting a newer refresh.
- Mutations set a per-item busy key; unrelated rows remain usable.
- Disconnect sets `phase: "stale"` and clears secret-bearing bridge fields. Data arrays are
  retained for context but cannot be acted on.

### 3.2 Clipboard state

```qml
property var clipboardState: ({
    phase: "loading",        // loading | empty | ready | stale | error | unavailable
    items: [],
    totalCount: 0,
    filter: "all",           // all | text | image | pinned
    search: "",
    detailId: "",
    detail: null,
    busyId: "",              // clip item id or "clear"
    lastLoadedAt: 0,
    errorCode: "",
    errorMessage: "",
    revision: 0
})
```

`phase` derivation:

| Condition | Phase |
|---|---|
| First request in flight and no items | `loading` |
| Request succeeded, zero items, no filter/search | `empty` |
| Request succeeded with items | `ready` |
| `ipc.connected === false` or `ipc.stale` | `stale` (takes precedence over data phases) |
| Request failed | `error` |
| Future `available: false` field | `unavailable` |

Until the backend adds availability metadata, `unavailable` is shown only when the request
itself fails (for example a future protocol/service error). A missing clipboard service still
returns an empty success today and is documented as limitation L3.

### 3.3 Shelf state

```qml
property var shelfState: ({
    phase: "loading",        // loading | empty | ready | stale | error | unavailable
    items: [],
    busyId: "",
    lastLoadedAt: 0,
    errorCode: "",
    errorMessage: "",
    revision: 0
})
```

Per-item status comes from `ShelfItem.stale` and `ShelfItem.availability`; it is not inferred
from the list phase. A stale item is still listed, with `Unavailable` status and no actions.

### 3.4 Bridge state

```qml
property var bridgeState: ({
    phase: "loading",        // loading | ready | stale | error | unavailable
    mode: "off",             // off | offering | confirming | paired | rejected | revoked | expired | error
    status: null,            // last BridgeStatus
    pendingPairing: null,    // PairingRequest or null
    transfers: [],           // PublicTransfer[]
    downloads: [],           // PublicDownload[]
    busyPairing: false,
    busyTransferId: "",
    busyDownloadId: "",
    lastLoadedAt: 0,
    errorCode: "",
    errorMessage: "",
    revision: 0
})
```

`mode` is derived from `status.state` but is never advanced locally. If `state` is
`confirming` and `pending_pairing` is null, the UI shows `expired` only after the backend
reports `expired`; until then it shows `confirming` with "Waiting for device details…" and no
approve control.

### 3.5 Derived read-only properties

| Property | Derivation |
|---|---|
| `clipboardItems` | `clipboardState.items` |
| `clipboardPhase` | `clipboardState.phase` |
| `shelfItems` | `shelfState.items` |
| `bridgeMode` | `bridgeState.mode` |
| `bridgePaired` | `mode === "paired"` and `status.active_device` object exists |
| `bridgeDeviceName` | `_text(active_device, "device_name", "")` |
| `bridgeSas` | `_text(pending_pairing, "sas_code", "")` |
| `bridgePairingExpiresAt` | `_number(pending_pairing, "expires_at", 0)` |
| `bridgeQrDataUrl` | `_text(status, "qr_data_url", "")` |
| `bridgeActiveTransfers` | transfers whose state is not terminal |
| `bridgeActiveDownloads` | downloads whose state is not terminal |

---

## 4. `KivoIpc` API Extensions

### 4.1 Clipboard methods

```qml
function clipboardGetHistory(options, callback)
// options: { limit?: 1..100, search?: string, filter?: "all"|"text"|"image"|"pinned" }
// Applies only if the request generation is current.
// Sets phase, items, totalCount, lastLoadedAt, error fields.

function clipboardGetItem(id, callback)
// Explicit detail request. Stores detailId/detail. Sensitive text arrives masked.

function clipboardCopyItem(id, callback)
// Sets busyId=id. Backend-confirmed. Emits clipboardOperationFinished("copy", ok, id).

function clipboardPinItem(id, pin, callback)
function clipboardDeleteItem(id, callback)
function clipboardClearHistory(callback)
// clear sets busyId="clear"; never locally removes pinned rows.
```

All methods call `request("clip_*", params, ...)` and normalise errors through one helper:

```qml
function _applySectionError(section, error) { ... }
```

### 4.2 Shelf methods

```qml
function shelfListItems(callback)
function shelfAddUris(uris, callback)
function shelfRemoveItem(id, callback)
function shelfClear(callback)
function shelfOpenItem(id, callback)   // wired but not exposed until remediation
```

`shelfAddUris` validates locally only for shape (array, 1–20, string); the backend remains
authoritative for URI policy and returns per-URI `rejected` entries. The UI reports both counts:

```text
"3 added · 1 rejected"
```

Rejected entries are displayed with `uri` as plain text in a bounded details list.

### 4.3 Bridge methods

```qml
function bridgeStart(port, callback)          // port optional; 1024–65535
function bridgeStop(callback)
function bridgeGetStatus(callback)
function bridgeApprovePairing(sessionId, callback)
function bridgeRejectPairing(sessionId, callback)
function bridgeRevokeDevice(sessionId, callback)
function bridgeGetTransfers(callback)
function bridgeResolveTransfer(transferId, decision, callback)
function bridgeCancelTransfer(transferId, callback)
function bridgeGetDownloads(callback)
function bridgeCreateDownloadTicket(itemId, callback)
// Internally always sends { item_id: itemId, authorize: true }.
// No caller can pass `authorize` as a variable.

function bridgeCancelDownload(downloadId, callback)
function bridgeGetDownloadHandoff(downloadId, callback)
// [PROPOSED PREREQUISITE] Requires an additive backend read operation.
// Returns { ok, handoff, error_code?, error_message? }.
```

### 4.4 Signals

```qml
signal clipboardOperationFinished(string operation, bool ok, string id)
signal shelfOperationFinished(string operation, bool ok, string id, int addedCount)
signal bridgePairingRequested(var request)
signal bridgePairingResolved(string outcome, string sessionId)
signal bridgeTransferChanged(var transfer)
signal bridgeDownloadChanged(var download)
signal bridgeOperationFinished(string operation, bool ok, string id)
signal productivityResyncStarted()
signal productivityResyncFinished()
```

Signals are emitted only after a backend response or event. The notch uses them for transients;
pages use them for targeted refreshes.

### 4.5 Event handlers

Add to the existing `switch (name)` in `_handleEvent`:

| Event | Handler behavior |
|---|---|
| `clip_history_changed` | `clipboardState.revision++`; schedule a 200 ms debounced `clipboardGetHistory` using the current filter/search |
| `shelf_items_changed` | `shelfState.revision++`; schedule a 200 ms debounced `shelfListItems` |
| `bridge_status_changed` | Apply status atomically; clear `pendingPairing` when state is not `confirming`; clear QR unless `offering`; refresh transfers/downloads on `paired`; emit transition signals |
| `bridge_pairing_requested` | Merge by `session_id` into `pendingPairing` (do not replace an object carrying `expires_at` from the preceding status event); emit `bridgePairingRequested` once per session; if no status payload has arrived, call `bridgeGetStatus()` |
| `bridge_transfer_requested/progress/completed` | Upsert transfer by `transfer_id`; sort by `created_at`; emit `bridgeTransferChanged` |
| `bridge_download_requested/progress/completed` | Upsert download by `download_id`; sort by `created_at`; emit `bridgeDownloadChanged` |

The existing duplicate/out-of-order drop stays in place. Upserts are idempotent, so a repeated
event cannot duplicate a row.

### 4.6 Connect, reconnect, gaps, restart

**On subscribe success / reconnect:** refresh all productivity sections after the existing
status/task refresh:

```text
clipboardGetHistory({ limit: 100 })
shelfListItems()
bridgeGetStatus()
bridgeGetTransfers()
bridgeGetDownloads()
```

Mark `productivityResyncStarted` before the first request and `productivityResyncFinished`
when all five callbacks have settled. Do not block the notch or existing pages.

**On disconnect:**

- `clipboardState.phase = "stale"`, `shelfState.phase = "stale"`, `bridgeState.phase = "stale"`.
- `bridgeState.pendingPairing = null`, `bridgeState.status.qr_data_url = ""`, SAS/QR properties
  become empty.
- All busy markers clear; pending callbacks fail with `connection_lost`.
- Approval cards remain visible only as stale, with every action disabled and an explicit
  "Disconnected — last known state" label. They are removed on the next authoritative status.

**On sequence gap:** set `productivityResyncPending = true` and schedule one 300 ms resync of
clipboard, shelf, bridge status, transfers, and downloads. Do not replay or infer missing
events.

**On backend restart:** reconnect resets sequences. The new backend has empty in-memory
clipboard/shelf/bridge state, so the UI replaces arrays with authoritative results. Any stale
QR/SAS is cleared on disconnect and never restored from cache.

**On duplicate events:** existing transport drops `sequence <= lastSequence`; ID upserts and
pairing dedupe provide a second layer.

**Expiry without an event:** the pairing manager cleans up expired offers, approvals, and
sessions lazily, and `PhoneBridgeService` does not publish a status event at the moment of
expiry. While `mode` is `offering`, `confirming`, or `paired`, poll `bridgeGetStatus()` every
30 s and once at each known deadline + 1 s (`pending_pairing.expires_at`,
`active_device.expires_at`). The status response is authoritative. Never move to `expired`
locally just because a countdown reached zero; show `Expired — checking…` and refresh.

The offering state has no offer-expiry timestamp in `BridgeStatus`, so the 30 s poll is the
only truthful way to notice offer expiry. This is a documented contract gap, not a reason to
invent a timer-driven terminal state.

---

## 5. DashboardClipboard

### 5.1 Layout

```text
┌ Clipboard ─────────────────────────────── [12 items] ┐
│ [Search………………………………]  (All) (Text) (Images) (Pinned) │
│ ┌───────────────────────────────────────────────────┐ │
│ │ Preview text, max 2 lines            [Sensitive]  │ │
│ │ 13:42 · 1.2 KB · text                  📌 Pin     │ │
│ └───────────────────────────────────────────────────┘ │
│ …                                                     │
│ Footer: history stored by cliphist on this device     │
└───────────────────────────────────────────────────────┘
```

- Search `TextField`: 36 px high, `surfaceSunken`, 12 px radius, placeholder
  "Search clipboard history". Debounce 250 ms. Backend limit 128 chars; enforce max length 128
  in the field.
- Filter chips: 28 px high, 8 px radius. Selected = `accentDim` background + `textPrimary`;
  unselected = `surfaceSunken` + `textSecondary`.
- Header count uses `total_count` from the response, not `items.length` (they are equal today,
  but the contract exposes both).
- `ListView` row height 64, spacing 8, `clip: true`.

### 5.2 Row content

| Element | Source | Rule |
|---|---|---|
| Preview | `item.preview` | Plain text, 2 lines max, `ElideRight`; sensitive preview is already masked |
| Sensitive badge | `item.is_sensitive` | Text `Sensitive`, `warning` border, never shows content |
| Type | `item.type` | `Text` or `Image` |
| Time | `item.timestamp` | Local `HH:mm`; relative for older than 24 h |
| Size | `item.size_bytes` | `formatBytes()` with one decimal for KiB+ |
| Pin marker | `item.is_pinned` | Filled pin glyph; no animation |
| Actions | row hover/focus | Details, Copy, Pin/Unpin, Delete |

Image items: no thumbnail is available over IPC. Show a neutral image-type badge and
`"Image · 1.4 MB"`; do not fetch raw image bytes.

### 5.3 Explicit detail

Clicking a row calls `clipboardGetItem(id)`. The detail panel (right-side sheet or inline
expansion; choose inline expansion at 680 px) shows:

- `text_content` in a read-only `TextArea`-like block, monospace for URLs? Use body font,
  selectable.
- For `is_sensitive`: `"Sensitive content is hidden. Copy requires an explicit confirmation."`
  and no text.
- `has_binary_data`: `"Binary content — preview unavailable."`
- Actions: `Copy item`, `Pin` (disabled for sensitive), `Delete`.

The detail panel is dismissed on page change, filter change, or item deletion. It never
auto-loads sensitive content into a preview.

### 5.4 Mutations and confirmations

| Action | Confirmation | Button label | After response |
|---|---|---|---|
| Copy normal | None | `Copy` | Toast "Copied"; refresh list |
| Copy sensitive | Confirm sheet: "Copy sensitive content? This writes the raw value to the system clipboard." | `Copy sensitive content` | Toast "Sensitive item copied"; refresh |
| Pin | None | `Pin` | Refreshed list; sensitive pin button disabled |
| Unpin | None | `Unpin` | Refreshed list |
| Delete | Confirm sheet with preview (masked if sensitive) | `Delete item` | Refreshed list |
| Clear history | Confirm sheet with count and pinned warning | `Clear clipboard history` | Refreshed list |

Clear confirmation copy must be accurate:

> This clears the entire cliphist database, including items KIVO has pinned. Pin metadata will
> remain, but pinned items may no longer be copyable. This cannot be undone.

The confirm button stays disabled until the checkbox "I understand pinned items will lose their
stored content" is checked. This is an explicit clear confirmation, not a generic dialog.

### 5.5 States

| State | Rendering |
|---|---|
| Loading | Three neutral placeholder rows, `"Loading clipboard history…"`; no fake text |
| Empty | Centered icon + `"No clipboard history"` + "Copy something to see it here." |
| Ready | List |
| Unavailable | Centered `"Clipboard service unavailable"` + retry; only on future flag/request failure |
| Transitioning | Row action buttons disabled; busy row shows "Copying…", "Pinning…", "Deleting…" |
| Stale | Amber banner `"Reconnecting — showing last known clipboard"`; all mutations disabled |
| Error | Inline banner with mapped message + Retry; previous items remain |
| Sensitive | Badge + masked preview + sensitive copy confirmation |
| Detail | Inline panel described above |

---

## 6. DashboardFileShelf

### 6.1 Layout

```text
┌ File Shelf ───────────────────────────── [3 files] ┐
│ [Add files…]                            [Clear all] │
│ ┌─────────────────────────────────────────────────┐ │
│ │ report-final-v7.pdf                    PDF · 2.4 MB │
│ │ staged 12m ago · Available        Remove  Open* │ │
│ └─────────────────────────────────────────────────┘ │
│ * Open unavailable until descriptor-safe launch exists │
└─────────────────────────────────────────────────────┘
```

`Open` is rendered disabled with an inline reason. It is not hidden: hiding it would make the
feature appear complete. It must not be clickable until L1 is fixed.

### 6.2 Add files

**[PROPOSED]** Use `QtQuick.Dialogs.FileDialog` (or the platform portal equivalent) with
`fileMode: FileDialog.OpenFiles`, `title: "Add files to KIVO File Shelf"`, and map
`selectedFiles` to `file://` URIs. If the dialog is unavailable at runtime, the button becomes
`Add files unavailable` with a reason and the page remains usable.

- Accept at most 20 files per batch; if more are selected, show "Maximum 20 files per batch"
  and do not send a partial fabricated success. Either send the first 20 with explicit copy, or
  ask the user to narrow the selection. Design choice: send no request and explain.
- Convert local paths with `Qt.url(path)`/`encodeURI`; do not hand-build `file://` strings.
- Report `added` and `rejected` separately. Each rejection shows `code` mapped to copy plus the
  URI as plain text (elided to 2 lines).

### 6.3 Row content

| Element | Source | Rule |
|---|---|---|
| Name | `item.name` | Plain text, 1 line, `ElideMiddle` to preserve extension |
| Type badge | `item.file_type` | Uppercase, 9 px, `surfaceHover` |
| Size | `item.size_bytes` | `formatBytes()` |
| Staged | `item.staged_at` | `"staged 3m ago"` |
| Availability | `item.availability`, `item.stale` | `Available` = accent, `Unavailable` = danger |
| Actions | — | `Send to phone`, `Remove`, `Open` (disabled) |

`Remove` only removes the reference; the file is untouched. Copy: `"Remove from shelf"` and a
toast `"Removed from shelf — file untouched"`. No confirmation is required for one item, but
`Clear all` requires a confirmation sheet.

### 6.4 Send-to-phone flow

1. `Send to phone` is enabled only when `bridgeMode === "paired"`.
2. If not paired, it opens the Bridge page with a non-destructive banner: "Pair a phone first."
3. Clicking it opens the **download authorization sheet**:
   - File name, size, type from the shelf item.
   - Target device name from `bridgeState.status.active_device.device_name`.
   - Heading: `"AUTHORIZE SENDING THIS FILE"`.
   - Checkbox: `"I choose to send this file to this device."`
   - Primary button: `Create download ticket`.
4. `bridgeCreateDownloadTicket(item.id)` sends `{ item_id, authorize: true }`.
5. On success, the download appears in `bridgeState.downloads` and the Bridge page opens the
   download section. The QR is shown only by an explicit `Show download QR` action.
6. On `stale_reference`, mark the shelf item stale and explain that the file changed.

No file bytes cross IPC. The ticket is authorization state only.

### 6.5 States

| State | Rendering |
|---|---|
| Loading | `"Loading File Shelf…"` placeholders |
| Empty | `"No files staged"` + "Add files to keep references here." |
| Ready | List |
| Unavailable | `"File Shelf service unavailable"` + retry |
| Transitioning | Row busy label (`Removing…`, `Authorizing…`); actions disabled |
| Stale backend | Amber banner; Add/Remove/Clear/Send disabled |
| Stale item | Row badge `Unavailable`, reason `"File changed, moved, or deleted"`; Remove only |
| Error | Inline banner with backend code/message; existing rows retained |

---

## 7. DashboardPhoneBridge

### 7.1 Layout

```text
┌ Phone Bridge ─────────────────────── [Paired / Off] ┐
│ PAIRING                                             │
│ ┌─────────────────────────────────────────────────┐ │
│ │   [QR 180×180]   Scan with KIVO on Android      │ │
│ │                  192.168.1.24:8443              │ │
│ │                  Fingerprint AB12 CD34 …        │ │
│ │                  Expires in 4:12   [Stop bridge]│ │
│ └─────────────────────────────────────────────────┘ │
│ DEVICE                                              │
│ ┌─────────────────────────────────────────────────┐ │
│ │ Pixel 9 · 192.168.1.31                          │ │
│ │ Paired 12:04 · Session expires 14:04  [Revoke]  │ │
│ └─────────────────────────────────────────────────┘ │
│ INCOMING FILES                                      │
│ … Gate A card / receiving / verifying / Gate B …    │
│ OUTGOING FILES                                      │
│ … downloads …                                       │
└─────────────────────────────────────────────────────┘
```

Use a `Flickable` with sections and 12 px gaps. The pairing and device sections are always
present (with state-appropriate content). Incoming and outgoing sections are hidden when the
bridge is off.

### 7.2 Pairing states

| `mode` | Content | Controls |
|---|---|---|
| `off` / `idle` | "Bridge is off. Start it to pair a phone on your local network." | `Start bridge` |
| `offering` | QR 180×180, `lan_ip:port`, fingerprint grouped in 4-char blocks, static note "The offer expires after 5 minutes" | `Stop bridge` |
| `confirming` | Device name, client IP, SAS in 21 px mono, approval countdown from `pending_pairing.expires_at` | `Approve device` + `Reject` |
| `paired` | Device card with name, IP, paired at, session expiry | `Revoke device` |
| `rejected` | "Pairing request rejected." | `Start new pairing` |
| `revoked` | "Device access revoked." | `Start new pairing` |
| `expired` | "Pairing offer or approval expired." | `Start new pairing` |
| `error` | `error_message` or generic failure | `Retry` |
| `stale` | Last known card, amber banner | All disabled |

`qr_data_url` is rendered only in `offering`. The image is decoded from the data URL; no file
is written. When the state leaves `offering`, the source is set to `""` immediately.

The fingerprint is public and can be shown. The SAS is secret-adjacent: it is displayed only
in `confirming`, never logged, never included in `snapshot()`, and cleared on disconnect.

### 7.3 Pairing approval — explicit sequence

1. The backend emits `bridge_pairing_requested` or `bridge_status_changed` with
   `pending_pairing`.
2. The card shows the device name and SAS. Device name is rendered as plain text with
   `textFormat: Text.PlainText`.
3. The service publishes `bridge_status_changed` before the request event, so the card uses
   `pending_pairing.expires_at` from that status. If only the request event is observed, the page
   calls `bridgeGetStatus()` and shows "Checking approval window…" with no approve control until
   the status arrives.
4. `Approve device` opens a confirmation sheet:
   - Heading: `APPROVE DEVICE PAIRING`
   - `"Does the code on your phone show 123 456?"`
   - Device name and IP repeated.
   - Checkbox: `"The code matches on both devices."`
   - Buttons: `Cancel` and `Approve device pairing`.
5. The confirm button stays disabled until the checkbox is checked.
6. `bridgeApprovePairing(session_id)` is sent. The UI shows `"Approving…"` and disables both
   actions. It does not change `mode` locally.
7. Only a `bridge_status_changed` with `state: "paired"` (or `bridgePairingResolved`) moves the
   page to the paired card and shows `"Device paired"`.

Rejection uses a separate confirmation sheet with `"Reject pairing request"` and a danger
outline button. There is no shared generic approval component.

### 7.4 Incoming uploads — Gate A and Gate B

Gate A and Gate B are different cards with different colors, headings, explanations, and
actions. They must never be collapsed into one "Approve" control.

**Gate A — permission to receive**

```text
[GATE A] INCOMING FILE — RECEIVE APPROVAL
Pixel 9 wants to send "holiday-photo.jpg" (4.2 MB).
Allowing starts the transfer into quarantine. It does not save the file to Downloads.
[Allow receiving]  [Reject file]
```

- Visible only for `state === "awaiting_desktop_approval"`.
- Gate badge: `warning` color, label `GATE A · RECEIVE`.
- Confirmation sheet heading: `ALLOW THIS DEVICE TO SEND?`
- Confirm label: `Allow receiving`.
- Countdown from `expires_at` (60 s). On expiry, the card disappears after the backend reports
  `expired`.
- No `accept_file` control on this card.

**Receiving / verifying**

```text
RECEIVING  42%  1.8 MB / 4.2 MB           [Cancel transfer]
VERIFYING  Checking file integrity…        [Cancel transfer]
```

- `receiving`: determinate progress from `received_bytes / size`; percent from
  `progress_percent` but recomputed locally from bytes if they disagree, and the bytes remain
  authoritative.
- `verifying`: indeterminate bar, no accept/discard buttons. Explanatory text: "The file has
  arrived and is being verified. You will be asked again before it is saved."

**Gate B — permission to publish**

```text
[GATE B] VERIFIED FILE — SAVE APPROVAL
"holiday-photo.jpg" passed verification (4.2 MB).
Saving publishes it to Downloads. This is the final step.
[Accept verified file]  [Discard file]
```

- Visible only for `state === "awaiting_final_acceptance"`.
- Gate badge: `accent` color, label `GATE B · SAVE`.
- Confirmation sheet heading: `SAVE THIS VERIFIED FILE?`
- Confirm label: `Accept verified file`.
- Discard confirmation label: `Discard file`.
- Countdown from `expires_at` (900 s).
- `accept_file` is never enabled before verification.

**Publishing (transitional UI-only state)**

The backend has no separate `publishing` state. Between the `accept_file` request and its
authoritative response/event, the card enters a local transitional state:

```text
PUBLISHING  Publishing "holiday-photo.jpg" to Downloads…
```

- Both Gate B actions are disabled; the progress area is indeterminate.
- The UI does not say "Saved" and does not remove the card.
- On a response with `state === "committed"`, show the terminal `Saved to Downloads as …` row.
- On a structured error, restore the Gate B card only if the returned state is still
  `awaiting_final_acceptance`; otherwise render the returned terminal state.
- On a timeout/disconnect, show stale/error and re-fetch; never assume publication succeeded.

**Terminal upload states**

| State | Copy | Action |
|---|---|---|
| `committed` | `Saved to Downloads as <published_filename>` | `Dismiss` |
| `rejected` | `Receive request rejected` | `Dismiss` |
| `discarded` | `Verified file discarded` | `Dismiss` |
| `cancelled` | `Transfer cancelled` | `Dismiss` |
| `expired` | `Approval or transfer expired` | `Dismiss` |
| `failed` | `Transfer failed` + mapped `error_code` | `Dismiss` |

`Dismiss` only clears the row from the local view after the backend marks it terminal; it does
not delete backend state. A refresh may legitimately show it again until terminal retention
(300 s) expires. The UI should not pretend dismissal is deletion.

### 7.5 Outgoing downloads

| State | Copy | Controls |
|---|---|---|
| `issued` | `Waiting for <device> to claim · expires in 4:32` | `Show download QR`*, `Cancel` |
| `claimed` | `Phone claimed the download` | `Cancel` |
| `streaming` | progress bar + bytes | `Cancel` |
| `completed` | `Sent to <device>` | `Dismiss` |
| `expired` | `Download ticket expired` | `Dismiss` |
| `revoked` | `Download revoked` | `Dismiss` |
| `cancelled` | `Download cancelled` | `Dismiss` |
| `failed` | `Download failed` + mapped code | `Dismiss` |

`*` **Blocked by limitation L2.** Until `bridge_get_download_handoff` exists, `Show download QR`
is rendered disabled with `"QR handoff unavailable in this build"`. The canonical payload must
come from the backend; the shell must not re-implement `kivo-download://v1` encoding.

QR overlay rules:

- 220 × 220 white quiet-zone panel with 12 px radius, centered in a scrim.
- Title: `SEND TO <DEVICE NAME>`.
- Warning: `"This code authorizes one download. Do not share it."`
- Auto-hide after 120 s, on state leaving `issued`, on page change, or on Escape.
- Never copy the payload to the clipboard and never log it.

### 7.6 Bridge section states

| State | Rendering |
|---|---|
| Loading | `"Loading bridge status…"` |
| Off | Start card; no QR, no device |
| Ready/paired | Pairing or device card + lists |
| Unavailable | `"Phone Bridge service unavailable"` + retry; no fake idle claim |
| Transitioning | Buttons replaced by `Starting…`, `Stopping…`, `Approving…`, `Revoking…` |
| Stale | Amber banner; pairing/transfer/download actions disabled |
| Error | Inline structured error + retry |

---

## 8. Shared Components

All components live in `shell/components/` and import `"../styles"`.

| Component | Purpose | Key constraints |
|---|---|---|
| `KivoSectionHeader.qml` | Page title + count + status badge | 28 px; title `fontTitle`; badge `fontMicro` |
| `KivoStateBadge.qml` | `Loading`, `Live`, `Stale`, `Error`, `Unavailable` | Token colors only |
| `KivoActionButton.qml` | Primary/neutral/danger buttons | Height 28 or 32; min width 96; `enabled` explicit; no default focus on destructive |
| `KivoFilterChip.qml` | Clipboard filters | 28 px; selected accentDim |
| `KivoEmptyState.qml` | Empty/loading/unavailable panel | Title + body + optional retry; no invented data |
| `KivoErrorBanner.qml` | Inline error | Shows mapped message, optional Retry |
| `KivoStaleBanner.qml` | Disconnected/stale | Amber; always says "last known" |
| `KivoConfirmSheet.qml` | Explicit confirmation overlay | Scrim, heading, body, checkbox, Cancel + confirm; confirm label is a required parameter, never defaults to "Approve" |
| `KivoProgressRow.qml` | Transfer/download progress | Determinate/indeterminate; bytes; cancel slot |
| `KivoGateBadge.qml` | Gate A / Gate B | `gate` enum; A=warning, B=accent; different labels |
| `KivoText.qml` | Plain-text rendering for untrusted strings | Forces `textFormat: Text.PlainText`, `wrapMode`, max lines |
| `NotchProductivityStrip.qml` | One-line notch status | 28 px; max one item; emits `reviewRequested(page)` |

`KivoConfirmSheet` intentionally has no default confirm text and no default `destructive`
value. Callers must supply the exact label. This prevents a future "Approve" catch-all.

---

## 9. Visual System

### 9.1 Surfaces and hierarchy

- Page background: transparent over `Theme.surfaceRaised` (dashboard body).
- Card: `Theme.surfaceSunken`, 12 px radius, 1 px `Theme.divider` border.
- Nested metadata strip: `Theme.surfaceRaised` or transparent; do not stack more than two
  surface levels.
- Hover: `Theme.surfaceHover`.
- Selected: `Theme.accentDim` + `Theme.accentSoft` border.
- Scrim: black at 0.55 opacity (local literal; no new theme token required).

### 9.2 Typography

| Use | Token |
|---|---|
| Page title | `fontTitle` 17, DemiBold |
| Card title / file name | `fontBody` 13, Medium |
| Metadata | `fontCaption` 10 |
| Badges / gate labels | `fontMicro` 9, DemiBold, letterSpacing 0.6 |
| SAS | `monoFamily` 21, Medium, letterSpacing 2.0 |
| Fingerprint / hashes | `monoFamily` 10 |
| Byte counts / timers | `monoFamily` 10 |

No new font family. No italics. No all-caps body text except badges and gate labels.

### 9.3 Buttons

| Kind | Background | Border | Text |
|---|---|---|---|
| Primary constructive | `accentDim` | `accentSoft` | `accentStrong` |
| Neutral | `surfaceRaised` | `border` | `textPrimary` |
| Destructive | transparent | `dangerDeep` | `danger` |
| Disabled | `surfaceSunken` | `divider` | `textFaint` |

Only one primary button per card. Gate A's constructive action uses warning accent, not the
same green as Gate B.

### 9.4 Dark/light

Only dark tokens exist. Stage 13.4 must not add a light switch or claim light support. All new
colors come from `Theme`. If a light palette is added later, it must be a separate design task
with its own acceptance tests; it is not part of 13.4.1–13.4.4.

---

## 10. Responsive and Stress Behavior

The dashboard is fixed at 880 × 620. "Responsive" means correct behavior inside the 680 × 540
host and under content stress, not a fluid window.

| Condition | Required behavior |
|---|---|
| 680 px host | Default layout |
| 560 px simulated width (test-only override) | Action rows wrap to two lines; no horizontal scroll; text elides |
| Filename 128 chars | One line, `ElideMiddle`, extension preserved; full name in detail/confirm sheet |
| Device name 128 chars | One line elide in rows; 2 lines wrap in pairing/confirm sheet |
| 50 clipboard rows | `ListView` virtualized; scrollbar; no layout height growth |
| 50 shelf rows | Same; count badge shows 50; Add disabled at capacity with reason "Shelf is full (50)" |
| 32 transfers + 32 downloads | Bounded `Repeater`; page scrolls; no nested scroll traps |
| 2 GiB file | `formatBytes` shows `2.0 GiB`; progress uses 64-bit-safe JS numbers |
| Empty history/shelf/transfer list | `KivoEmptyState`; no fake rows |
| Missing backend service | Error/unavailable state, retry; no fabricated "0 items" success claim |
| Long error message | 2 lines max in banner, full text in tooltip/expanded error |
| Long URIs in rejection list | 2 lines, `ElideMiddle`, plain text |

Input constraints:

- Clipboard search max 128 chars.
- Shelf batches 1–20; more selected → no request + explanation.
- Bridge port 1024–65535; default 8443; invalid input disables Start.
- No free-text device names or filenames are ever sent to the backend; display only.

---

## 11. Notch Integration

### 11.1 Constraints

- The notch is a fixed-height capsule; `expandedHeight` is computed in `DynamicNotch.qml`.
- Media, telemetry, mic, and PTT already occupy the body. New content must not push them out
  or cause overlap.
- The collapsed notch has no room for new chips.
- The notch must never become a second approval surface for Gate A/B.

### 11.2 Expanded notch

Add one optional `NotchProductivityStrip` (28 px) between the transcript summary and the
context line. Add 36 px (28 + 8 spacing) to `expandedHeight` only when it is visible, and never
show it in `driving`/`stopping` states.

Visibility and priority (only one at a time):

| Priority | Condition | Copy | Action |
|---:|---|---|---|
| 1 | `confirming` with pending pairing | `Pairing request · <device>` | `Review` → Bridge |
| 2 | Upload `awaiting_desktop_approval` | `Incoming file · <name>` | `Review` → Bridge |
| 3 | Upload `awaiting_final_acceptance` | `Verified · <name>` | `Review` → Bridge |
| 4 | Upload `receiving` / `verifying` | `Receiving 42% · <name>` / `Verifying · <name>` | `Review` → Bridge |
| 5 | Download `issued`/`claimed`/`streaming` | `Sending 42% · <name>` | `Review` → Bridge |
| 6 | `bridgeMode === "error"` | `Phone Bridge error` | `Review` → Bridge |
| 7 | Nothing active | Strip hidden | — |

The strip is not a button row. `Review` is a text affordance that emits
`reviewRequested("bridge")`; the shell opens the dashboard on the Bridge page. The strip never
contains `Approve`, `Accept`, `Allow`, or `Revoke`.

Clipboard and shelf get **no** expanded row. Their surfaces are the dashboard pages.

### 11.3 Collapsed notch

No new persistent chip. Do not change `collapsedWidth` for clipboard, shelf, or bridge state.
A pairing request may switch the collapsed label through the existing transient mechanism, but
must not widen the capsule beyond the current computed width.

### 11.4 Transients

Use the existing `showTransient(state, title, detail, duration, autoCollapse)` API. Add
listeners in `DynamicNotch` for the new `KivoIpc` signals. Every transient is backend-confirmed
and deduplicated by ID.

| Trigger | State | Copy | Duration | Conditions |
|---|---|---|---:|---|
| Clipboard copy success | `success` | `Copied` / `Clipboard item copied` | 1800 ms | No preview, no sensitive text |
| Shelf add success | `success` | `Files staged` / `2 files added to File Shelf` | 2200 ms | Only if `addedCount > 0` |
| Shelf add all rejected | `warning` | `Nothing added` / `1 file rejected` | 3000 ms | No URI echo in the notch |
| Pairing requested | `warning` | `Pairing request` / `Open Dashboard to review` | 5000 ms | No auto-expand; never shows SAS |
| Pairing approved | `success` | `Device paired` / `<device name>` | 2200 ms | Backend status transition only |
| Pairing rejected/expired | `neutral` | `Pairing ended` / `No device was paired` | 2600 ms | — |
| Upload Gate A requested | `warning` | `Incoming file` / `Open Dashboard to review` | 5000 ms | No filename if longer than 40 chars? Show elided name |
| Upload committed | `success` | `File received` / `<published_filename>` | 2600 ms | `state === "committed"` only |
| Upload failed/discarded/expired | `neutral`/`error` | `Transfer ended` / mapped reason | 3000 ms | Terminal event only |
| Download completed | `success` | `Download complete` / `<filename>` | 2400 ms | `state === "completed"` only |
| Download revoked/expired | `warning` | `Download ended` / mapped reason | 3000 ms | Terminal event only |

Media priority: if media is active and the notch is expanded, do not replace the media drawer
with a transient. Show the productivity strip instead. If the notch is collapsed, media state
wins over non-urgent transients (clipboard copy, shelf add); pairing and Gate A requests may
still show a transient because they are time-sensitive.

Deduplication: keep `_lastProductivityTransientKey` = `event + id`; ignore the same key within
5 s. This mirrors the existing terminal-result deduplication.

### 11.5 Notch acceptance rules

- No fabrication: a transient appears only after a response/event.
- No SAS, QR, handoff payload, full path, raw clipboard text, or device IP in the notch.
- No approval action in the notch.
- No change to media, telemetry, mic, or PTT visibility rules.
- `KIVO_NOTCH_REDUCED_MOTION=1` disables all new animations; the strip appears instantly.

---

## 12. Cross-Cutting State Matrix

| State | Clipboard | Shelf | Bridge |
|---|---|---|---|
| Loading | Placeholder rows + text | Placeholder rows + text | `Loading bridge status…` |
| Empty | `No clipboard history` | `No files staged` | `Bridge off` (idle) |
| Ready | List | List | Pairing/device + lists |
| Unavailable | Service error + retry | Service error + retry | Service error + retry |
| Transitioning | Per-row busy | Per-row busy | Starting/stopping/approving/revoking |
| Stale | Amber banner, actions disabled | Same | Same; QR/SAS cleared |
| Error | Banner + retry | Banner + retry | Banner + retry |
| Approval | n/a | Download authorization sheet | Pairing / Gate A / Gate B |
| Receiving | n/a | n/a | Determinate progress + cancel |
| Verifying | n/a | n/a | Indeterminate; no accept |
| Acceptance | n/a | n/a | Gate B card only |
| Publishing | n/a | n/a | `Publishing to Downloads…` while `accept_file` is in flight; no success claim until `committed` |
| Completed | Copy/pin/delete done | Add/remove done | `committed` / `completed` |
| Cancelled | n/a | n/a | Terminal, reason shown |
| Expired | n/a | n/a | Offer/approval/session/ticket expired |

---

## 13. Architecture Verdict

The design is implementable with the existing single `KivoIpc` scope and existing backend
operations, except for two prerequisites:

1. Replace pathname `xdg-open` with descriptor-safe `OpenFile` before enabling `Open`.
2. Add an additive download-handoff read operation before enabling the download QR.

Both are specified in `STAGE_13_4_UI_SECURITY_REVIEW.md` and sequenced in
`STAGE_13_4_UI_IMPLEMENTATION_ROADMAP.md`.
