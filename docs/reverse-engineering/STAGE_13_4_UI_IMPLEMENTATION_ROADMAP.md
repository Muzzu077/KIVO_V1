# KIVO — Stage 13.4 UI Implementation Roadmap

```
Document:   Stage 13.4 UI Implementation Roadmap
Status:     Proposed sequencing — implementation not started
Date:       2026-11-11
Baseline:   HEAD 1062c0d; audit and specifications in the other three Stage 13.4 documents
Scope:      Stages 13.4.1–13.4.4, prerequisites, exit gates, and the complete test matrix
```

---

## 1. Objective and Non-Goals

### 1.1 Objective

Deliver the desktop surfaces for three already-implemented backends through the single
existing `KivoIpc` connection:

- `DashboardClipboard`
- `DashboardFileShelf`
- `DashboardPhoneBridge`
- restrained notch status and completion feedback

### 1.2 Non-goals

- No backend architecture rewrite, new daemon, second socket, or second shell process.
- No change to the IPC framing, version, socket permissions, or event sequence rules.
- No Android client change in Stage 13.4 unless a desktop contract fix requires it.
- No real pairing, listener, transfer, or clipboard database mutation during implementation
  tests. Runtime verification uses injected fakes and loopback-free unit tests.
- No commit or push as part of this planning stage.
- No light theme, no window resizing redesign, no dashboard layout rewrite.

---

## 2. Prerequisites

| ID | Prerequisite | Blocks | Owner stage | Decision |
|---|---|---|---|---|
| P1 | Expose the canonical download handoff payload: add `bridge_get_download_handoff` (preferred) or a `handoff` field on ticket creation | Download QR in 13.4.2 | 13.4.2 backend slice | Required |
| P2 | Add `available`/`error_code` to clipboard history, shelf list, and bridge status when the service is absent | Accurate unavailable states | 13.4.1 / 13.4.2 | Recommended; if deferred, limitation L3 remains |
| P3 | Replace Shelf `xdg-open` pathname launch with descriptor-safe `OpenFile` | Enabling `Open` | 13.4.1 security slice | Required before `Open` |
| P4 | Decide the Add Files mechanism (`QtQuick.Dialogs.FileDialog`/portal, drag/drop, or no picker) | Shelf add flow | 13.4.1 | Required |
| P5 | Confirm the running portal supports `OpenURI.OpenFile` and choose the D-Bus path (binding or `busctl` with `pass_fds`) | P3 | 13.4.1 | Required for `Open` |
| P6 | Confirm QML data-URL image rendering for the pairing QR at 180–220 px | Pairing UI | 13.4.2 | Runtime probe |
| P7 | Freeze the four Stage 13.4 documents as the implementation reference | All | 13.4.0 | This stage |

**P1 and P3 are hard gates for the features they protect. The rest of Stage 13.4 can proceed
without exposing those features.**

---

## 3. Stage 13.4.1 — Clipboard and File Shelf Dashboard

### 3.1 Goal

Ship `DashboardClipboard` and `DashboardFileShelf`, the shared state sections in `KivoIpc`, and
the reusable components they need. Keep Shelf `Open` disabled unless P3 is complete.

### 3.2 Deliverables

| File | Change |
|---|---|
| `shell/services/KivoIpc.qml` | Add `clipboardState`, `shelfState`, section generations, clipboard/shelf methods, event handlers, reconnect resync, gap resync, signals |
| `shell/components/DashboardClipboard.qml` | New page |
| `shell/components/DashboardFileShelf.qml` | New page |
| `shell/components/KivoSectionHeader.qml` | New shared component |
| `shell/components/KivoStateBadge.qml` | New shared component |
| `shell/components/KivoActionButton.qml` | New shared component |
| `shell/components/KivoFilterChip.qml` | New shared component |
| `shell/components/KivoEmptyState.qml` | New shared component |
| `shell/components/KivoErrorBanner.qml` | New shared component |
| `shell/components/KivoStaleBanner.qml` | New shared component |
| `shell/components/KivoConfirmSheet.qml` | New shared component |
| `shell/components/KivoText.qml` | New plain-text enforcement component |
| `shell/components/KivoDashboard.qml` | Add two nav items and two page instances |
| `shell/tests/test_clipboard_ui.py` | New tests for state/requests/events (pure QML logic exercised through the existing shell test harness where possible) |
| `shell/tests/test_shelf_ui.py` | New tests |
| `kivo/shelf/service.py` + portal helper | P3 only: descriptor-safe open; keep `Open` disabled until tests pass |
| `kivo/ipc/handlers.py` + `protocol.py` | P2 only: `available` fields; no new operations for P3 if the helper is internal |

### 3.3 Clipboard acceptance criteria

1. History loads with `limit: 100` on page open and on reconnect.
2. Search is debounced 250 ms, capped at 128 chars, and sends `search` with the active filter.
3. Filters map exactly to `all|text|image|pinned`.
4. Sensitive previews are masked; the UI never displays raw sensitive text.
5. Sensitive copy requires the confirmation sheet and a checked acknowledgement.
6. Sensitive pin is disabled with an explanation; the backend rejection is also handled.
7. Delete requires confirmation; clear requires the pinned-content acknowledgement.
8. Count-only events refresh the list; no item payload is invented.
9. Disconnect shows the stale banner and disables every mutation.
10. No clipboard content, preview, or `raw_id` appears in logs or `snapshot()`.

### 3.4 Shelf acceptance criteria

1. List renders `id`, `name`, `file_type`, `size_bytes`, `staged_at`, `stale`, `availability`
   only.
2. Add sends 1–20 `file://` URIs and reports `added` and `rejected` separately.
3. Duplicate staging does not create a duplicate row.
4. Capacity `50` disables Add with a reason.
5. Remove says "Remove from shelf" and confirms the disk file is untouched.
6. Clear requires confirmation and does not delete files.
7. Stale/unavailable rows show the status and offer Remove only.
8. `Open` is disabled with the exact reason until P3 passes. No pathname launch is reachable
   from QML.
9. Send-to-phone is disabled unless the bridge is paired; when enabled it opens the explicit
   authorization sheet from 13.4.2.

### 3.5 Exit gate

| Check | Requirement |
|---|---|
| Backend tests | `.venv/bin/python -m pytest tests/ -q` passes |
| Shell tests | `.venv/bin/python -m pytest shell/tests/ -q` passes |
| QML lint | `/usr/lib/qt6/bin/qmllint` on all changed/new QML: zero errors |
| Desktop health | `./scripts/desktop-health.sh`: 9/9 PASS |
| Whitespace | `git diff --check` clean |
| Manual | Clipboard and shelf state matrix rows pass |
| Security | No `Open` action enabled; no raw clipboard text in UI/logs |

---

## 4. Stage 13.4.2 — Phone Bridge Authorization and Transfer Dashboard

### 4.1 Goal

Ship `DashboardPhoneBridge`: pairing, device state, Gate A, receiving, verifying, Gate B,
downloads, and QR delivery. Resolve P1 before enabling download QR.

### 4.2 Deliverables

| File | Change |
|---|---|
| `shell/services/KivoIpc.qml` | Add `bridgeState`, pairing/transfer/download methods, event handlers, dedupe, reconnect resync, signals |
| `shell/components/DashboardPhoneBridge.qml` | New page |
| `shell/components/KivoGateBadge.qml` | Gate A/B badge |
| `shell/components/KivoProgressRow.qml` | Determinate/indeterminate progress with cancel |
| `shell/components/KivoDashboard.qml` | Add bridge nav item and page instance |
| `shell/qr/` or `shell/components/PairingQrPanel.qml` | QR rendering panel |
| `kivo/bridge/download_handoff` exposure | P1: `bridge_get_download_handoff` or `handoff` field + protocol validator + tests |
| `shell/tests/test_bridge_ui.py` | New state-machine tests |
| `tests/test_bridge_download_handoff_ipc.py` | P1 contract tests |

### 4.3 Pairing acceptance criteria

1. `idle` shows Start bridge; no QR and no device.
2. `offering` shows the QR, address, fingerprint, and Stop.
3. `confirming` shows device name, IP, SAS, countdown, `Approve device`, `Reject`.
4. Approval requires the SAS-match checkbox and the exact button `Approve device pairing`.
5. Rejection uses a separate confirmation and label.
6. `paired` shows device name, IP, paired time, session expiry, and `Revoke device`.
7. Revocation waits for the backend status; no local paired→off transition.
8. `rejected`, `revoked`, `expired`, and `error` each show their own copy and a recovery action.
9. QR, SAS, token, and fingerprint secrets never enter logs or `snapshot()`.
10. Disconnect clears QR/SAS and disables all actions.

### 4.4 Upload acceptance criteria

1. Gate A card appears only for `awaiting_desktop_approval`.
2. Gate A heading, badge, explanation, and button are exactly the Gate A set.
3. Gate A sends `allow_receive` or `reject_receive` only.
4. Receiving shows bytes and percent; Cancel is available.
5. Verifying shows an indeterminate state and **no** accept/discard control.
6. Gate B appears only for `awaiting_final_acceptance`.
7. Gate B heading, badge, explanation, and button are exactly the Gate B set.
8. Gate B sends `accept_file` or `discard_file` only.
9. The in-flight `accept_file` UI says `Publishing to Downloads…` and never claims success
   before the backend reports `committed`.
10. `committed` shows the backend `published_filename` as plain text.
11. No code path sends `accept_file` before verification.

### 4.5 Download acceptance criteria

1. Create ticket requires a paired device, a shelf item, and the explicit send sheet with a
   checked acknowledgement.
2. `KivoIpc` always sends `authorize: true`; the caller cannot override it.
3. `issued`, `claimed`, `streaming`, `completed`, `expired`, `revoked`, `cancelled`, and
   `failed` render distinct states.
4. `Show download QR` is disabled until P1 is complete; after P1 it shows the backend payload
   only.
5. QR auto-hides after 120 s, on state change, on page change, and on Escape.
6. Cancel waits for terminal state.
7. `get_downloads` refresh after reconnect shows `download_id`/`device_id` bindings without
   claiming shelf association (limitation L4).

### 4.6 Exit gate

| Check | Requirement |
|---|---|
| Backend tests | Full suite passes, including new handoff IPC tests |
| Shell tests | Full suite passes, including the bridge state-machine tests |
| QML lint | Zero errors |
| Desktop health | 9/9 PASS |
| Whitespace | `git diff --check` clean |
| Manual | Gate A/B distinction table reproduced exactly; no generic approve exists |
| Security | No secret in logs, QR only in its panel, no pathname fallback |

---

## 5. Stage 13.4.3 — Restrained Notch Productivity

### 5.1 Goal

Add one compact productivity strip and backend-confirmed transients without disturbing media,
telemetry, mic, or PTT.

### 5.2 Deliverables

| File | Change |
|---|---|
| `shell/components/NotchProductivityStrip.qml` | New 28 px row |
| `shell/components/DynamicNotch.qml` | Visibility priority, height delta, transient listeners, dedupe, review signal |
| `shell/shell.qml` | `openDashboardPage(page)` and notch signal wiring |
| `shell/tests/test_notch.py` | Extended tests |

### 5.3 Acceptance criteria

1. The strip is hidden unless one of the priority conditions exists.
2. Exactly one item is shown at a time, in the documented priority order.
3. The strip never contains an approval action.
4. The strip never shows SAS, QR, handoff, path, IP, or raw clipboard text.
5. `Review` opens the Bridge page; it does not start, stop, approve, or cancel anything.
6. `expandedHeight` increases by exactly 36 px when the strip is visible, and the footer does
   not overlap.
7. The strip is suppressed in `driving`/`stopping`.
8. Media, telemetry, mic, and PTT visibility rules are unchanged.
9. Collapsed width is unchanged by all three features.
10. Transients are emitted only after backend confirmation and are deduplicated by ID.
11. Reduced motion disables strip/transient animation.

### 5.4 Exit gate

| Check | Requirement |
|---|---|
| Backend tests | Unchanged full suite passes |
| Shell tests | Notch tests pass, including height and priority |
| QML lint | Zero errors |
| Desktop health | 9/9 PASS |
| Manual | Media playing + pairing request + transfer does not crowd or hide media controls |
| Visual | Strip uses only `Theme` tokens and matches existing spacing |

---

## 6. Stage 13.4.4 — End-to-End Desktop Integration Testing

### 6.1 Goal

Validate the complete desktop-side flow, reconnect behavior, visual stress, and security
matrix. Physical Android validation is attempted if a device is available; otherwise it is
recorded as deferred.

### 6.2 Work

1. Start the real backend and shell in a controlled session.
2. Exercise clipboard history, filters, sensitive copy confirmation, pin rejection, delete,
   and clear acknowledgement.
3. Exercise shelf add (picker), duplicate, rejection, capacity, remove, clear, stale file, and
   disabled Open.
4. Start the bridge on loopback/LAN only if permitted; otherwise use the existing unit-level
   state fakes. **Do not perform a real transfer in this stage unless the operator explicitly
   provides a device and approval.** The default is no listener and no pairing.
5. Drive pairing states through a fake backend/harness to verify each card and transition.
6. Simulate disconnect, reconnect, sequence gap, duplicate event, backend restart, and
   revocation through tests, not by killing production services.
7. Run the full UI, notch, security, resilience, and visual matrices below.
8. Record physical-device status and defer if `adb devices` is empty.

### 6.3 Exit gate

| Check | Requirement |
|---|---|
| Backend tests | 100% pass |
| Shell tests | 100% pass |
| QML lint | Zero errors |
| Desktop health | 9/9 PASS |
| Whitespace | `git diff --check` clean |
| Matrices | All rows PASS or explicitly DEFERRED with a reason |
| Verdict | `READY WITH LIMITATIONS` unless P1/P3 and physical validation are complete |

---

## 7. Complete UI Test Matrix

### 7.1 Clipboard

| ID | Scenario | Expected |
|---|---|---|
| UI-CLIP-01 | Open page, backend connected | Loading then ready; no fake rows |
| UI-CLIP-02 | Empty history | Empty state with guidance |
| UI-CLIP-03 | 50 items | Virtualized list; scroll; count correct |
| UI-CLIP-04 | Search "token" | Debounced request with `search`; results match backend |
| UI-CLIP-05 | Filter Text/Images/Pinned | Exact filter values; count updates |
| UI-CLIP-06 | Sensitive row | Masked preview + Sensitive badge |
| UI-CLIP-07 | Sensitive detail | Masked text; no raw content in QML |
| UI-CLIP-08 | Copy normal | `clip_copy_item`; toast only after `ok:true` |
| UI-CLIP-09 | Copy sensitive | Confirmation required; raw copy only after confirm |
| UI-CLIP-10 | Pin sensitive | Disabled with reason; backend rejection also handled |
| UI-CLIP-11 | Delete item | Confirmation; refreshed list |
| UI-CLIP-12 | Clear unchecked | Confirm disabled |
| UI-CLIP-13 | Clear checked | One request; pinned warning; refreshed list |
| UI-CLIP-14 | Clear result with pinned rows | Rows remain; copy failure explained |
| UI-CLIP-15 | `clip_history_changed` | One debounced refresh; no duplicate request storm |
| UI-CLIP-16 | Disconnect | Stale banner; all actions disabled |
| UI-CLIP-17 | Request error | Error banner + retry; old items remain |
| UI-CLIP-18 | Search 129 chars | Input capped; no invalid request |
| UI-CLIP-19 | 128-char preview | Two-line elide; no overflow |
| UI-CLIP-20 | Image item | Image badge; no raw image fetch |

### 7.2 File Shelf

| ID | Scenario | Expected |
|---|---|---|
| UI-SHELF-01 | Open page | Loading then ready |
| UI-SHELF-02 | Empty shelf | Empty state |
| UI-SHELF-03 | Add 1 file | `added` count; row appears |
| UI-SHELF-04 | Add 20 files | All accepted/rejected reported |
| UI-SHELF-05 | Add 21 files | No request; explanation |
| UI-SHELF-06 | Duplicate URI | No duplicate row |
| UI-SHELF-07 | 50 items | Add disabled with capacity reason |
| UI-SHELF-08 | Mixed accepted/rejected | Both counts and rejection reasons |
| UI-SHELF-09 | Stale item | Unavailable badge; Remove only |
| UI-SHELF-10 | Remove item | "Remove from shelf"; file untouched |
| UI-SHELF-11 | Clear all | Confirmation; all references removed |
| UI-SHELF-12 | Open action | Disabled; no request; no pathname launch |
| UI-SHELF-13 | Send to phone, not paired | Bridge page guidance; no ticket |
| UI-SHELF-14 | Send to phone, paired | Authorization sheet; checkbox required |
| UI-SHELF-15 | Ticket creation success | Download appears in Bridge page |
| UI-SHELF-16 | Ticket creation stale | Item marked stale; truthful error |
| UI-SHELF-17 | 128-char filename | Middle elide; extension preserved |
| UI-SHELF-18 | 2 GiB file | Size formatting correct |
| UI-SHELF-19 | `shelf_items_changed` | One debounced refresh |
| UI-SHELF-20 | Disconnect | Stale banner; Add/Remove/Clear disabled |

### 7.3 Phone Bridge — pairing and device

| ID | Scenario | Expected |
|---|---|---|
| UI-BR-01 | Bridge off | Start card; no QR |
| UI-BR-02 | Start success | `offering`; QR + address + fingerprint |
| UI-BR-03 | Start failure (no LAN) | Error copy; no fake offer |
| UI-BR-04 | Offer expiry | 30 s status poll returns `expired`; QR removed; no local timer terminal |
| UI-BR-05 | Pairing request | Device + SAS; no auto-approve |
| UI-BR-06 | Approve unchecked | Confirm disabled |
| UI-BR-07 | Approve checked | Exact method + label; "Approving…" |
| UI-BR-08 | Approve success | Paired card only after status event |
| UI-BR-09 | Approve failure | Error; request remains actionable or expires |
| UI-BR-10 | Reject | Separate confirmation; rejected state |
| UI-BR-11 | Session expiry | Deadline + poll returns `expired`; no stale actions |
| UI-BR-12 | Revoke | "Revoking…" until status; device removed |
| UI-BR-13 | QR after handshake | `qr_data_url` empty; image source cleared |
| UI-BR-14 | Disconnect while confirming | SAS/QR cleared; actions disabled |
| UI-BR-15 | Backend restart | Status refetched; no stale pending card |
| UI-BR-16 | 128-char device name | Elided in card; full plain text in confirm |
| UI-BR-17 | HTML-like device name | Rendered literally; no rich text |
| UI-BR-18 | Fingerprint | Grouped hex; not truncated |
| UI-BR-19 | Port invalid | Start disabled; backend not called |
| UI-BR-20 | Stop bridge | Off state; transfers/downloads cleared by backend |

### 7.4 Phone Bridge — uploads

| ID | Scenario | Expected |
|---|---|---|
| UI-UP-01 | Gate A pending | Warning badge, receive copy, two actions |
| UI-UP-02 | Gate A allow unchecked | Confirm disabled |
| UI-UP-03 | Gate A allow checked | `allow_receive`; "Starting receive…" |
| UI-UP-04 | Gate A reject | `reject_receive`; rejected card |
| UI-UP-05 | Receiving | Determinate progress + Cancel |
| UI-UP-06 | Verifying | Indeterminate; no accept/discard |
| UI-UP-07 | Gate B pending | Accent badge, save copy, two actions |
| UI-UP-08 | Gate B accept unchecked | Confirm disabled |
| UI-UP-09 | Gate B accept checked | `accept_file`; `Publishing…` while in flight; committed only after response |
| UI-UP-10 | Gate B discard | `discard_file`; discarded card |
| UI-UP-11 | Gate B before verifying | No control rendered |
| UI-UP-12 | Committed | `published_filename` plain text |
| UI-UP-13 | Failed | Mapped error code + dismiss |
| UI-UP-14 | Expired approval | Card removed after backend expiry |
| UI-UP-15 | Cancel transfer | Transitional then terminal |
| UI-UP-16 | Revoked session | Transfer cancelled by backend; no local success |
| UI-UP-17 | Long filename | Elided; full in Gate A/B sheet |
| UI-UP-18 | 2 GiB upload | Bytes and percent correct |
| UI-UP-19 | Duplicate progress event | Row upserted, not duplicated |
| UI-UP-20 | Generic approve scan | No `Approve`-only control in any upload state |

### 7.5 Phone Bridge — downloads

| ID | Scenario | Expected |
|---|---|---|
| UI-DL-01 | No session | "Pair a phone" guidance |
| UI-DL-02 | Create without paired session | Button disabled |
| UI-DL-03 | Create without acknowledgement | Confirm disabled |
| UI-DL-04 | Create success | `issued` row; countdown |
| UI-DL-05 | QR before P1 | Disabled with reason |
| UI-DL-06 | QR after P1 | Backend payload rendered; auto-hide |
| UI-DL-07 | QR payload logging | Absent from logs/snapshot |
| UI-DL-08 | Claimed | Claimed label; no chunk claims |
| UI-DL-09 | Streaming | Progress + cancel |
| UI-DL-10 | Completed | Completed copy; dismiss |
| UI-DL-11 | Ticket expiry | Expired; QR hidden |
| UI-DL-12 | Cancel | Transitional then cancelled |
| UI-DL-13 | Revocation | Revoked by backend; no local transition |
| UI-DL-14 | Reconnect | Downloads refetched; states correct |
| UI-DL-15 | `item_id` absence | No shelf association claimed |
| UI-DL-16 | Duplicate event | Upsert only |
| UI-DL-17 | Long filename/device | Plain text, elided, full in QR panel |
| UI-DL-18 | 120 s QR timeout | QR closes; ticket remains until expiry |
| UI-DL-19 | Escape with QR | QR closes; no state change |
| UI-DL-20 | Stale backend | Actions disabled; QR cleared |

---

## 8. Notch Test Matrix

| ID | Scenario | Expected |
|---|---|---|
| NOTCH-01 | No productivity activity | No strip; height unchanged |
| NOTCH-02 | Pairing request | Strip priority 1; "Review" opens Bridge |
| NOTCH-03 | Gate A + receiving | Gate A wins priority |
| NOTCH-04 | Gate B + download | Gate B wins priority |
| NOTCH-05 | Receiving only | Progress copy |
| NOTCH-06 | Download only | Sending copy |
| NOTCH-07 | Bridge error | Error strip |
| NOTCH-08 | Strip visible | `expandedHeight` +36; footer no overlap |
| NOTCH-09 | Driving/stopping | Strip suppressed |
| NOTCH-10 | Media active | Media drawer remains; strip does not replace it |
| NOTCH-11 | Telemetry/mic/PTT | Visibility rules unchanged |
| NOTCH-12 | Collapsed | Width unchanged; no new chip |
| NOTCH-13 | Clipboard copy | "Copied" transient after success only |
| NOTCH-14 | Shelf add | Added count transient; no URI echo |
| NOTCH-15 | Upload committed | "File received" after committed only |
| NOTCH-16 | Download completed | "Download complete" after completed only |
| NOTCH-17 | Duplicate completion | One transient within 5 s |
| NOTCH-18 | Failed transfer | Error transient; no success |
| NOTCH-19 | Reduced motion | No strip/transient animation |
| NOTCH-20 | Disconnect | Strip hides; no stale approval offered |
| NOTCH-21 | SAS/QR/path scan | None appear in notch |
| NOTCH-22 | Approval scan | No approval control in notch |

---

## 9. Security Test Matrix

| ID | Scenario | Expected |
|---|---|---|
| SEC-01 | Shelf open path replacement | Portal receives original fd; replacement not opened |
| SEC-02 | Shelf open symlink replacement | Symlink never followed |
| SEC-03 | Shelf open hard-link swap | Original fd identity retained |
| SEC-04 | Shelf open portal unavailable | `portal_unavailable`; no `xdg-open` fallback |
| SEC-05 | Shelf open executable | Refused with `unsafe_file_type` |
| SEC-06 | Shelf open stale identity | Marked stale; no portal call |
| SEC-07 | Shelf open argv scan | No production pathname launch remains |
| SEC-08 | Sensitive preview | Masked |
| SEC-09 | Sensitive detail | Masked |
| SEC-10 | Sensitive pin | Rejected |
| SEC-11 | Sensitive copy | Explicit confirmation only |
| SEC-12 | Clipboard event | Count only |
| SEC-13 | Clipboard raw bytes over IPC | None |
| SEC-14 | AI agent clipboard access | No tool exists; regression assertion |
| SEC-15 | Clear history | Pinned collateral warning + acknowledgement |
| SEC-16 | Upstream cliphist disclosure | Visible in Clipboard footer |
| SEC-17 | Pairing QR logging | Absent |
| SEC-18 | SAS logging | Absent |
| SEC-19 | Handoff logging | Absent |
| SEC-20 | Token/key IPC scan | Absent from all public payloads |
| SEC-21 | Gate A wrong state | Backend rejection; no UI success |
| SEC-22 | Gate B wrong state | Backend rejection; no UI success |
| SEC-23 | Generic approve scan | None in pairing/Gate A/Gate B |
| SEC-24 | Stale approval disabled | Cannot submit while stale |
| SEC-25 | Old session ID | Rejected/disabled after refresh |
| SEC-26 | Revocation | Backend cancels; UI waits |
| SEC-27 | Remote filename HTML | Plain text only |
| SEC-28 | Remote device name HTML | Plain text only |
| SEC-29 | Path in public payload | Absent |
| SEC-30 | Socket permissions | 0600 file / 0700 directory unchanged |

---

## 10. Resilience and Lifecycle Matrix

| ID | Scenario | Expected |
|---|---|---|
| RES-01 | Socket disconnect | All three sections stale; actions disabled; QR/SAS cleared |
| RES-02 | Reconnect | Status/lists refreshed; sequences reset; no stale approvals |
| RES-03 | Event sequence gap | One debounced resync of all sections |
| RES-04 | Duplicate event | Dropped by transport; upsert idempotent |
| RES-05 | Out-of-order event | Dropped; newer state retained |
| RES-06 | Backend restart | Empty in-memory lists shown truthfully; no cached secrets |
| RES-07 | Clipboard count event during search | Refresh preserves filter/search |
| RES-08 | Shelf count event during removal | Refresh reflects backend truth |
| RES-09 | Bridge status event during approval | Approval card replaced by authoritative state |
| RES-10 | Transfer event after page close | State retained; no UI crash |
| RES-11 | Download event after page close | State retained; QR not auto-opened |
| RES-12 | Cancel timeout | "Still cancelling…" after 5 s; no success claim |
| RES-13 | Revocation during Gate B | Card removed; no publish claim |
| RES-14 | Session expiry during Gate A | Card removed; no receive claim |
| RES-15 | Dashboard close/reopen | Data refetched; secrets not retained in QML |
| RES-16 | Notch expanded during update | No height jump beyond +36; footer stable |

---

## 11. Visual and Stress Matrix

| ID | Scenario | Expected |
|---|---|---|
| VIS-01 | Default 680×540 host | Layout fits; no clipping |
| VIS-02 | Simulated 560 px width | Actions wrap; no horizontal scroll |
| VIS-03 | 50 clipboard items | List scrolls; header fixed |
| VIS-04 | 50 shelf items | List scrolls; Add disabled at 50 |
| VIS-05 | 32 transfers + 32 downloads | Page scrolls; no nested scroll trap |
| VIS-06 | 128-char names | Elide/wrap rules hold |
| VIS-07 | Empty states | Centered, no fake values |
| VIS-08 | Error/stale banners | Do not overlap header or actions |
| VIS-09 | Dark tokens only | No light/invented colors |
| VIS-10 | Font scale +1 step | No clipping; rows grow or elide |
| VIS-11 | Reduced motion | No decorative animation |
| VIS-12 | Keyboard focus | Tab order logical; destructive default not focused |
| VIS-13 | Long confirmation copy | Fits sheet; scroll if needed |
| VIS-14 | Gate A/B side by side comparison | Visually and textually distinct |
| VIS-15 | QR at 180 and 220 px | Scannable, not stretched, quiet zone intact |

---

## 12. Backend Regression Matrix

| ID | Suite | Requirement |
|---|---|---|
| REG-01 | `tests/test_clipboard_*` | Pass unchanged |
| REG-02 | `tests/test_shelf_*` | Pass; P3 adds new tests |
| REG-03 | `tests/test_bridge_pairing.py` | Pass unchanged |
| REG-04 | `tests/test_bridge_transfer*.py` | Pass unchanged |
| REG-05 | `tests/test_bridge_download*.py` | Pass; P1 adds handoff IPC tests |
| REG-06 | `tests/test_bridge_ipc.py` | Pass; new operations allowlisted |
| REG-07 | `tests/test_stage12_2_regression.py` | Pass; no agent shelf/clipboard tools |
| REG-08 | Stage 12 media/telemetry/mic tests | Pass unchanged |
| REG-09 | Shell notch tests | Pass; new cases added |
| REG-10 | Desktop health | 9/9 PASS |

---

## 13. Verification Commands

Run from the repository root with the project virtual environment:

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m pytest shell/tests/ -q
/usr/lib/qt6/bin/qmllint shell/shell.qml shell/components/*.qml shell/services/*.qml
./scripts/desktop-health.sh
git diff --check
```

Expected Stage 13.4.4 result:

- Backend: 100% pass.
- Shell: 100% pass.
- QML lint: exit 0, zero errors.
- Desktop health: 9/9 PASS.
- `git diff --check`: clean.

Physical-device validation, if available:

```bash
adb devices
```

If the list is empty, record physical pairing/transfer validation as **DEFERRED** and keep the
`READY WITH LIMITATIONS` verdict.

---

## 14. Delivery Sequence and Dependencies

```text
13.4.0  Audit + four documents                     [this stage]
   │
   ├─ P3/P5 (descriptor-safe open) ──► enables Shelf Open (optional in 13.4.1)
   ├─ P2 (availability flags) ───────► improves unavailable states (optional)
   │
13.4.1  Clipboard + File Shelf UI                  [depends on P4 for Add Files]
   │
13.4.2  Phone Bridge UI                            [depends on P1 for download QR]
   │
13.4.3  Notch productivity                         [depends on 13.4.1/13.4.2 signals]
   │
13.4.4  Desktop end-to-end + matrices              [physical device after desktop pass]
```

No stage may begin by modifying a later stage's files. Each stage lands with tests and passes
the exit gate before the next begins.

---

## 15. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| QML file-dialog unavailable in Quickshell | Medium | Add Files blocked | Probe in 13.4.1; fallback to drag/drop decision or explicit unavailable |
| Portal `OpenFile` unsupported | Medium | Open blocked | Keep Open disabled; no pathname fallback |
| D-Bus fd passing complexity | Medium | Open delayed | Use `busctl` with inherited fd or a minimal binding; test in 13.4.1 |
| Handoff contract rejected | Low | Download QR blocked | Keep QR disabled; ship rest of 13.4.2 |
| Empty/unavailable ambiguity persists | Medium | Misleading empty state | P2 or explicit limitation copy |
| QML performance with 50 rows | Low | Jank | `ListView` virtualization |
| Notch height regression | Medium | Overlap/crowding | Exact +36 delta test and footer assertion |
| Secret in logs | Low | Security regression | Log scan tests, no payload logging |
| Gate copy drift | Medium | Safety regression | Gate A/B copy constants + review |
| No physical device | High | Unverified E2E | Defer with explicit verdict |

---

## 16. Verified Behavior vs Proposed Design

### 16.1 Verified in this stage

- Existing dashboard pages, fixed geometry, theme tokens, and nav structure.
- Existing `KivoIpc` transport, reconnect, sequence, duplicate, and gap behavior.
- Clipboard operations, masking, pin rejection, count events, clear behavior, and upstream
  cliphist persistence.
- Shelf operations, opaque IDs, stale detection, and the `open_item()` pathname race.
- Bridge operations, status/pairing/transfer/download payloads, authorization chain, Gate A/B
  states, TTLs, revocation, cancellation, and path-free public payloads.
- Download handoff builder exists but is not exposed.
- No clipboard/shelf/bridge wiring exists in QML.
- No AI-agent clipboard or shelf tool exists.

### 16.2 Proposed only

- Three dashboard pages and their layouts.
- `KivoIpc` state sections, methods, signals, and event handlers.
- Reusable components and plain-text enforcement.
- Descriptor-safe `OpenFile` remediation and its tests.
- Download handoff IPC exposure.
- Availability metadata.
- Notch strip, transients, height delta, and review routing.
- All test matrices and acceptance criteria.

Nothing in 16.2 is implemented by this document.

---

## 17. Final Verdict

**READY WITH LIMITATIONS.**

Reasons:

1. **P3/S1:** Shelf `Open` is blocked by a confirmed TOCTOU until descriptor-safe launching is
   implemented and race-tested.
2. **P1/S2:** Download QR is blocked until the canonical handoff payload is exposed over IPC.
3. **L3:** Empty and unavailable remain indistinguishable without availability metadata.
4. **L5:** The shell is dark-only; light-mode support is not part of this design.
5. **L10:** No physical Android device/emulator is available, so pairing and transfer remain
   unverified end to end.

With those limitations accepted, Stages 13.4.1–13.4.4 are ready to begin in order, each behind
its own exit gate. No production file was changed in Stage 13.4.0; only these four documents
were created.
