# KIVO — Stage 13.4 UI Security & Authorization Review

```
Document:   Stage 13.4 UI Security Review
Status:     Audit + remediation specification
Date:       2026-11-11
Baseline:   HEAD 1062c0d
Scope:      File Shelf open race, clipboard privacy, Phone Bridge authorization chain,
            Gate A/Gate B separation, and secret-handling rules for the Stage 13.4 UI
Mode:       Read-only audit. No production code, protocol, or clipboard database changed.
```

---

## 1. Executive Summary

The backend security model is substantially stronger than the current UI needs, but one
high-severity issue blocks a planned UI action and one medium-severity contract gap blocks the
download QR:

| ID | Finding | Severity | Status |
|---|---|---|---|
| S1 | `shelf_open_item()` validates a descriptor, closes it, then launches `xdg-open` by pathname | **High** | Open must remain disabled |
| S2 | Download handoff payload exists but is not exposed over IPC | Medium | Download QR unavailable |
| S3 | Empty and unavailable responses are indistinguishable | Medium | UI cannot always state the truth |
| S4 | `clip_clear_history()` also destroys pinned raw content | Medium | Must be stated in confirmation |
| S5 | Upstream cliphist stores content before KIVO filters it | High, upstream | Irreducible; must be disclosed |
| S6 | Remote device names/filenames are untrusted display text | Medium | Must force plain text |
| S7 | Pairing QR and download handoff are capability secrets | Medium | Never log/cache; explicit reveal |
| S8 | Same-user process can connect to the 0600 IPC socket | Low, out of scope | Same-user trust boundary |

No secret leakage was found in the existing IPC payloads or clipboard events. Clipboard
masking, sensitive-pin exclusion, and the absence of any AI-agent clipboard tool are verified.
The Phone Bridge authorization chain is coherent and backend-enforced at every step.

**UI security verdict: READY WITH LIMITATIONS.** Stage 13.4.1 may expose clipboard and shelf
listing/removal/clear, but not `Open`. Stage 13.4.2 may expose pairing, Gate A, receiving,
verifying, and Gate B. The download QR must wait for S2 to be resolved by an additive backend
contract.

---

## 2. Scope and Method

### 2.1 Verified surfaces

- `kivo/shelf/service.py`, `path_policy.py`, `models.py`
- `kivo/clipboard/service.py`, `models.py`, `pin_store.py`, `runner.py`, `privacy.py`
- `kivo/bridge/*` including pairing, transfer, download, download handoff, quarantine, server
- `kivo/ipc/handlers.py`, `kivo/ipc/protocol.py`
- `kivo/app.py` event publication and lifecycle
- `shell/services/KivoIpc.qml`, `shell/shell.qml`, `shell/components/*`
- Existing tests for shelf, clipboard, bridge, and notch

### 2.2 Out of scope

- Android client internals except where they define the desktop authorization contract.
- Kernel/filesystem hardening beyond the Shelf path policy.
- Same-user process compromise, physical device compromise, or TLS implementation review.
- Live pairing, listener, or transfer execution. This stage does not start a bridge, open a
  socket, pair a device, or move a file.

---

## 3. File Shelf `open_item()` — TOCTOU

### 3.1 Verified vulnerable sequence

```text
lock: item = _items[item_id]
verify_existing_identity(item._canonical_path, item._identity)
    └─ opens path with O_NOFOLLOW, checks dev/ino/size/mtime/mode/mnt_id
    └─ closes descriptor
if _xdg_open is None: fail
run_bounded([xdg_open, item._canonical_path], timeout=2.0)
```

The identity check and the launch resolve the pathname at different times. The check is not a
capability; it is a point-in-time assertion. An attacker who can modify the parent directory
can replace the path after the check.

### 3.2 Attack scenarios

| Scenario | Preconditions | Result |
|---|---|---|
| Symlink replacement | Parent directory writable by another process/user | `xdg-open` follows the replacement to a sensitive file |
| Hard-link replacement | Attacker can create a hard link to a sensitive file on the same filesystem | Handler opens sensitive content under a benign display name |
| File swap | Download/sync client rewrites the file after validation | User opens different content than reviewed |
| Executable/desktop swap | Replacement is executable or a `.desktop` file | `xdg-open`/portal may execute it instead of viewing it |
| Shared directory | `/tmp`, shared project dir, synced folder | Increases replacement opportunities; sticky bit does not prevent the owner or same-user processes |

The download quarantine path already uses retained descriptors and `renameat2(RENAME_NOREPLACE)`
for publication. The Shelf open path is the outlier.

### 3.3 Remediation options evaluated

#### Option A — Portal `OpenURI.OpenURI` with a `file://` URI

Rejected as the sole fix. The portal receives a URI, not a descriptor, and resolves it when it
processes the request. The replacement window shrinks but does not close. It also inherits the
same pathname trust problem.

#### Option B — Portal `OpenURI.OpenFile` with a verified UNIX file descriptor

**Recommended.** The backend reopens the item through the existing `O_NOFOLLOW` authorizer,
verifies the exact staged identity, retains that descriptor, and passes it as a D-Bus
`UNIX_FD` (`h`) to `org.freedesktop.portal.OpenURI.OpenFile` on the user session bus. The portal
duplicates the open file description. Subsequent path replacement cannot change which inode the
portal reads. This is the smallest change that closes the race while preserving "open the
original file" semantics.

Requirements:

1. `OpenFile` must be supported by the running portal (interface version ≥ 3). Probe once at
   startup or first use; if unsupported, return `open_unavailable`.
2. Keep the descriptor open until the D-Bus method call returns. The portal has duplicated it
   by then; close the local copy afterwards.
3. Use an empty `parent_window` string unless the backend is given a valid surface handle.
   Options may include `ask: true` so the portal asks the user which application to use.
4. **No fallback to `xdg-open`, `gio open`, or any pathname launch on portal failure.**
5. Return structured errors: `portal_unavailable`, `portal_unsupported`, `open_failed`,
   `stale_reference`.
6. The UI keeps `Open` disabled until this path and its tests are complete.

Implementation note: the current virtual environment has no Python D-Bus binding. A runtime
probe must choose one of:

- a minimal D-Bus client that supports UNIX_FD (`dbus-next`/`jeepney`), or
- `busctl call` with the verified descriptor inherited by the child (`pass_fds`) and passed as
  the `h` argument.

The choice is an implementation detail for Stage 13.4.1. The non-negotiable rule is descriptor
handoff, not pathname fallback.

#### Option C — Private immutable copy, then open the copy

Safe if the copy is created in a KIVO-owned 0700 directory with `O_CREAT|O_EXCL`, fully written
and fsynced from the verified descriptor before launch. It avoids pathname replacement because
the attacker cannot write to KIVO's private directory.

Rejected as the primary fix because it changes semantics: applications edit a copy, not the
original, and 2 GiB files would be duplicated. It is acceptable only as an explicitly labelled
fallback ("Open a temporary copy") if the portal is unavailable. Stage 13.4.1 should not
implement it by default.

#### Option D — Restrict writable/shared directories or symlinks only

Necessary but insufficient. The shelf already rejects symlinks during authorization
(`O_NOFOLLOW`) and blocks sensitive system prefixes. Blocking group/world-writable parent
directories reduces exposure, but:

- The user's Downloads directory is normally writable by the user and by sync/browser
  processes.
- A same-user process can still replace the file.
- The check itself is another point-in-time assertion.

These restrictions are defense in depth, not a race fix.

#### Option E — Inherit the descriptor into a custom launcher

Possible but fragile. `xdg-open` does not accept a descriptor. A custom launcher could pass
`/proc/self/fd/N` to the target application with `pass_fds`, but desktop-file association,
application sandboxing, and child lifetime make this unreliable. Rejected for Stage 13.4.

### 3.4 Smallest safe remediation package

1. Keep the verified `AuthorizedFileDescriptor` open through the launch call.
2. Call `org.freedesktop.portal.OpenURI.OpenFile` with that descriptor.
3. Revalidate identity immediately before the call; close and mark stale on mismatch.
4. Refuse executable regular files (`mode & 0o111`) and known executable/desktop extensions
   (`.desktop`, `.sh`, `.AppImage`), with a clear error. This is defense in depth.
5. Never fall back to a pathname launch.
6. Keep the dashboard `Open` action disabled until the above ships and the race test passes.

### 3.5 Open remediation tests

| Test | Expected |
|---|---|
| `test_open_uses_descriptor_not_path` | Portal client receives the verified fd; no `xdg-open` argv is constructed |
| `test_open_replacement_after_validation` | Path replaced after descriptor acquisition; portal fd still refers to the original inode |
| `test_open_symlink_replacement` | Replacement symlink is never followed; original fd is used |
| `test_open_portal_unavailable` | Returns `portal_unavailable`; no fallback subprocess |
| `test_open_portal_unsupported_version` | Returns `portal_unsupported`; no fallback |
| `test_open_executable_refused` | Executable file and `.desktop` return `unsafe_file_type` |
| `test_open_identity_mismatch` | Marks item stale; no portal call |
| `test_open_helper_argv_never_contains_path` | Assert no production pathname launch remains |

---

## 4. Clipboard Security Review

### 4.1 Verified protections

| Control | Evidence | Result |
|---|---|---|
| Sensitive preview masking | `ClipItem.to_dict()` uses the privacy filter's masked preview | PASS |
| Sensitive detail masking | `ClipDetail.to_dict()` returns `"••••••••••••"` | PASS |
| Sensitive pin exclusion | `ClipboardService.pin_item` raises `ValueError` | PASS |
| Pin storage hardening | JSON at `$XDG_DATA_HOME/kivo/clipboard_pins.json`, mode 0600, atomic replace | PASS |
| Pin metadata-only | Pin store stores `ClipItem` metadata, not raw bytes | PASS |
| Event secrecy | `clip_history_changed` carries `{"count": int}` only | PASS |
| IPC raw-data exclusion | History/detail return text metadata, never raw clipboard bytes over IPC | PASS |
| Agent access | No clipboard tool or `clip_*` reference in agent/tool packages | PASS |
| Automatic copy | No code path copies without an explicit `clip_copy_item` request | PASS |
| Explicit clear | `clip_clear_history` is a distinct operation; no automatic caller | PASS |
| Log leakage | No clipboard content/preview is logged by handlers, service, or runner | PASS |

### 4.2 Sensitive copy is intentionally allowed

`clip_copy_item()` decodes the raw cliphist entry and calls `wl-copy` even when the item is
sensitive. This is a deliberate explicit action, not a leak: the user must request that specific
opaque ID. The UI must make it a distinct, confirmed action:

- Sensitive rows show `Sensitive` and a masked preview.
- `Copy` becomes `Copy sensitive content`.
- A confirmation sheet states that the raw value will be written to the system clipboard.
- No drag, keyboard shortcut, or row double-click may copy automatically.

### 4.3 Clear-history collateral

`ClipboardService.clear_history()` calls `cliphist wipe`, which removes **all** raw entries. The
docstring says "unpinned", but pinned raw content is destroyed too. Pin metadata survives and
pinned rows remain visible, but their content can no longer be decoded or copied.

UI requirements:

1. Confirmation shows the current item count and states the pinned consequence.
2. A checkbox acknowledgement is required before the destructive button enables.
3. Button label is `Clear clipboard history`, not `Clear`.
4. The result reports the backend response; no optimistic list clearing.
5. After success, refresh. Pinned rows remain; copy failures are shown as
   `"Content is no longer available"` rather than a generic error.

### 4.4 Upstream cliphist limitation

`wl-paste --watch cliphist store` writes clipboard content to `~/.cache/cliphist/db` before any
downstream process can inspect it. `cliphist 0.7.0` has no exclusion or regex filter. KIVO can
mask the UI, refuse pins, and avoid IPC leakage, but it cannot guarantee that a secret was never
written by the upstream watcher.

Required disclosure:

> Clipboard history is stored by the system cliphist watcher on this device. KIVO masks
> sensitive items in its interface and never sends raw clipboard content over IPC, but it cannot
> prevent cliphist from storing a value before KIVO sees it.

This disclosure belongs in the Clipboard page footer and must not be buried in Settings. The UI
must not claim "zero-trace" or "secret-safe clipboard".

### 4.5 Clipboard test matrix

| Test | Expected |
|---|---|
| Sensitive history preview | Masked; no raw text in `items` |
| Sensitive detail | `text_content` masked |
| Sensitive pin request | Rejected; UI disables Pin |
| Sensitive copy request | Requires explicit confirmation; backend receives the exact opaque ID |
| History event payload | Contains only `count` |
| Clear confirmation unchecked | Confirm button disabled |
| Clear confirmation checked | Single `clip_clear_history` request; waits for response |
| Clear result | Pinned rows remain; copy failure handled truthfully |
| Disconnected clear | Button disabled; no local deletion |
| Log scan | No preview/text/raw bytes in logs or `snapshot()` |

---

## 5. Phone Bridge Authorization Review

### 5.1 Chain — verified step by step

| # | Step | Backend enforcement | UI obligation |
|---:|---|---|---|
| 1 | Bridge start | Physical LAN interface, ephemeral cert, port 1024–65535 | Show bind address/fingerprint; no "internet" claim |
| 2 | Offer | 5-minute TTL, 32-byte secret, server nonce, session ID | Show QR only in `offering`; clear when it ends |
| 3 | QR | `kivo-pair://v1` with addr, fp, secret, id, nonce, exp | Render as image; never log/copy payload |
| 4 | Fingerprint | Ephemeral ECDSA P-256 cert fingerprint in QR and status | Display grouped hex; do not truncate in confirm view |
| 5 | Handshake | HMAC proof, version, nonce length, per-IP/global limits, lockout | Show structured failure; no retry loop |
| 6 | SAS | Derived code on both sides, stored in `pending_pairing.sas_code` | Display exact code; require match checkbox |
| 7 | Desktop approval | `bridge_approve_pairing(session_id)`, 60 s TTL, exact pending match | Explicit `Approve device pairing`; no generic approve |
| 8 | Rejection | `bridge_reject_pairing(session_id)` | Separate danger action; confirmation |
| 9 | Session | 2-hour TTL, bearer token/keys never in IPC | Show expiry; no token display |
| 10 | Revocation | `bridge_revoke_device(session_id)` revokes and cancels transfers/downloads | Wait for `bridge_status_changed`; show "Revoking…" |
| 11 | Upload offer | `awaiting_desktop_approval` | Gate A card |
| 12 | Upload Gate A | `allow_receive` only from pending; reserves capacity | `Allow receiving`; explicit confirmation |
| 13 | Receiving | Valid session + transfer; chunk hash checks | Progress + Cancel; no accept |
| 14 | Verifying | All bytes received; full-file SHA-256 | Indeterminate; no accept/discard |
| 15 | Upload Gate B | `accept_file` only from `awaiting_final_acceptance`; publishes to Downloads | Separate Gate B card; `Accept verified file` |
| 16 | Discard | `discard_file` only from final acceptance | Separate danger action |
| 17 | Download authorization | `bridge_create_download_ticket` requires literal `authorize: true` and a shelf ID | Explicit send sheet + checkbox |
| 18 | Download ticket | 5-minute redemption; bound to session/device/file | Show claim countdown; no local reuse |
| 19 | Claim | Claim before chunks; 30-minute lease; 30-second idle timeout | Show claimed/streaming truthfully |
| 20 | Download QR | Handoff payload `kivo-download://v1?...` with ticket/device/name/size/sha | **Blocked by S2**; explicit reveal only |
| 21 | Cancellation | Transfer/download cancel returns updated state | Show "Cancelling…" until terminal |
| 22 | Expiry | Offer, approval, session, acceptance, ticket, lease, idle | Terminal card; no revive |

### 5.2 Gate A and Gate B are distinct — mandatory

| Dimension | Gate A | Gate B |
|---|---|---|
| Question | May this device send the file? | May this verified file be saved? |
| State | `awaiting_desktop_approval` | `awaiting_final_acceptance` |
| Badge | `GATE A · RECEIVE`, warning color | `GATE B · SAVE`, accent color |
| Heading | `INCOMING FILE — RECEIVE APPROVAL` | `VERIFIED FILE — SAVE APPROVAL` |
| Explanation | "Allowing starts the transfer into quarantine. It does not save the file." | "Saving publishes it to Downloads. This is the final step." |
| Primary label | `Allow receiving` | `Accept verified file` |
| Secondary label | `Reject file` | `Discard file` |
| Backend decision | `allow_receive` / `reject_receive` | `accept_file` / `discard_file` |
| TTL | 60 s | 900 s |
| Available during receiving/verifying | No | No |
| Generic "Approve" allowed | **No** | **No** |

A shared `KivoConfirmSheet` may provide layout, but each gate supplies its own heading, body,
confirm label, and color. The component has no default confirm label. Code review must reject
any call site that passes `"Approve"`.

### 5.3 Download QR — capability handling

The pairing QR is a secret-bearing payload. The download handoff is a capability reference:
it does not itself authorize chunks (the phone still needs the bearer token and `K_up` MAC),
but possession plus a paired device can redeem the ticket. Treat it as sensitive.

Rules:

- The canonical payload comes from the backend (`build_handoff_for_ticket`), never re-encoded
  in QML.
- Render only on explicit `Show download QR`.
- Auto-hide after 120 s, on state leaving `issued`, on page change, and on Escape.
- Never log, never place in `snapshot()`, never copy to clipboard.
- Do not cache in a file or QML `Settings`.
- The QR panel states the target device and "This code authorizes one download."
- The handoff is not shown in the notch.

### 5.4 Revocation and cancellation semantics

- Revocation is a backend action that also cancels the device's transfers and downloads. The UI
  must not locally mark them cancelled; it waits for the status/transfer/download events.
- Cancellation responses may return an updated object, but the terminal state remains the
  backend's. The UI shows a transitioning label until the terminal state is observed.
- Expiry can happen while the dashboard is closed. On reconnect, status/list refresh is
  authoritative; stale approval cards must disappear.
- Approval actions are bound to the displayed `session_id`/`transfer_id`; if the card's ID no
  longer matches the latest status, the action is disabled and replaced by a refreshed card.

### 5.5 Untrusted display text

Untrusted fields:

- `PairingRequest.device_name`
- `PairedSession.device_name`
- `UploadTransfer.filename`
- `UploadTransfer.published_filename`
- `DownloadTicket.filename`
- `DownloadTicket.device_name`
- rejection `uri` strings
- `error_code`/`error_message` from remote-triggered failures

QML requirements:

- Always `textFormat: Text.PlainText` (QML `Text` defaults to `AutoText`, which can interpret
  HTML-like content).
- Bound length with `maximumLineCount` and `elide`.
- No `Text.RichText`, no `onLinkActivated`, no dynamic `Loader` from these strings.
- No path separators are interpreted; filenames are display text only.
- No tooltip that renders rich text.
- Use `KivoText` so the rule is enforced once.

### 5.6 Bridge test matrix

| Test | Expected |
|---|---|
| Offer expiry | UI moves to `expired` only after backend state; QR cleared |
| QR absent outside offering | `bridgeQrDataUrl` empty; no cached image |
| SAS display | Exact `sas_code`, plain text, only in `confirming` |
| Approval without checkbox | Confirm disabled |
| Approval response delayed | "Approving…"; no local paired state |
| Rejection | `reject_receive`? separate pairing reject; terminal copy |
| Revocation | Wait for status; transfers/downloads cancelled by backend |
| Gate A wrong state | `bridge_resolve_transfer(allow_receive)` fails `invalid_transfer_state` |
| Gate A accept before Gate B | No accept control in receiving/verifying |
| Gate B before verification | Backend rejects; UI never offers it |
| Gate B accept | `committed` only after response; `published_filename` plain text |
| Discard | `discarded`; no Downloads claim |
| Download ticket without authorize | Protocol rejects; UI always sends literal true |
| Download claim | Issued/claimed/streaming labels correct |
| Ticket expiry | Terminal `expired`; QR hidden |
| Cancellation | Transitional then terminal |
| Revoked session | All approvals disabled; no stale action |
| Long device name | One line elided; full plain text in confirm sheet |
| HTML-like filename | Rendered literally, no rich text |
| Log scan | No SAS, QR, handoff, token, key, path |

---

## 6. Secret-Handling Rules

### 6.1 Values classified as secrets

| Value | Source | Handling |
|---|---|---|
| Pairing QR data URL | `BridgeStatus.qr_data_url` | Render only while offering; never log/copy/cache |
| Pairing SAS | `pending_pairing.sas_code` | Display only in confirmation; clear on disconnect |
| Download handoff | Future IPC field | Explicit reveal; auto-hide; never log/copy |
| Bearer token / session keys | Not exposed | Must never be added to IPC |
| Raw clipboard text | Local backend only | Never in events; detail only on explicit request |
| Sensitive clipboard preview | Masked by backend | Never attempt to unmask in QML |
| Certificate private key | Never exposed | Must never appear in status/logs |
| File paths / inodes | Internal | Never added to public IPC |

### 6.2 Logging policy

- Do not log request params for `bridge_*`, `clip_*`, or `shelf_*`.
- Do not log `qr_data_url`, `sas_code`, `handoff`, `text_content`, or rejection URIs.
- `KivoIpc` protocol warnings may include event names and sequence numbers, never payloads.
- Do not extend `IpcHandler.snapshot()` with bridge/clipboard/shelf values.
- Error messages from the backend may be shown but not appended with local secrets.

### 6.3 Screenshot and accessibility exposure

- The QR and SAS are visible on screen by design; the UI cannot prevent screenshots.
- The QR overlay should not be part of the notch or a persistent page state.
- Accessible names must not include the SAS or handoff payload; use "Pairing code" for the
  accessible label and keep the visible code separate.
- Clipboard item accessible names must not include sensitive previews.

### 6.4 IPC trust boundary

The socket is mode 0600 inside a 0700 directory. Any process running as the same user can
connect and issue requests. This is the existing KIVO trust model. Stage 13.4 must not claim
protection against a compromised same-user process. It must also not widen access: no TCP
listener for UI IPC, no world-readable files, no shell command construction from remote text.

---

## 7. Authorization UX Invariants

These are testable requirements, not style preferences:

1. No control labelled only `Approve`, `OK`, or `Yes` may authorize pairing, receiving, saving,
   sending, or revoking.
2. Every authorization sheet names the exact action, device, and file (where applicable).
3. Every destructive or authorizing confirmation starts with the confirm control disabled.
4. The confirm control is enabled only by an explicit acknowledgement (checkbox) or by a
   two-step reveal, never by focus or Enter alone.
5. Cancel is always available and is the default focus for destructive sheets.
6. The UI never changes approval state locally; it waits for a response/event.
7. A stale or disconnected card cannot be approved.
8. Approval IDs are re-read from the latest state; a replaced card cannot submit an old ID.
9. The notch never authorizes anything.
10. Only one authorization request per ID may be in flight.
11. The backend's structured rejection is shown; it is never converted into success.
12. No approval is remembered across reconnects or backend restarts.

---

## 8. Residual Risks

| Risk | Residual level | Reason |
|---|---|---|
| Shelf path replacement | High until S1 fixed | Pathname launch |
| Secret already in cliphist DB | High, upstream | Watcher writes before KIVO |
| QR shoulder-surfing | Medium | Visible by design; short TTL limits reuse |
| Malicious device name phishing | Medium | SAS match + approval; plain text prevents markup injection |
| Same-user IPC attacker | Low/out of scope | 0600 socket trust boundary |
| Download ticket leakage via QR | Medium | Capability reference; session binding and TTL limit |
| Stale UI approval | Low after rules | Backend rejects wrong state; UI disables stale |
| Log leakage | Low | No secret logging; tests required |
| Physical device absence | Validation gap | No end-to-end pairing test available |

---

## 9. Security Verdict

**READY WITH LIMITATIONS.**

- Clipboard privacy and clear semantics are implementable now, with explicit copy/clear
  confirmations and the upstream cliphist disclosure.
- Shelf listing/removal/clear is implementable now. `Open` is **blocked** until the descriptor
  handoff is implemented and race-tested.
- Pairing, Gate A, receiving, verifying, Gate B, cancellation, revocation, and expiry are
  implementable now with distinct, explicit controls.
- Download QR is **blocked** until the backend exposes the canonical handoff payload.
- Physical pairing/transfer remains unverified without a device.
