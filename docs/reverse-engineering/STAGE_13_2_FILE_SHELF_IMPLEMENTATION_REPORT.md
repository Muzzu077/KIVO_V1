# KIVO — Stage 13.2 File Shelf Backend & IPC Implementation Report

```
Document:   Stage 13.2 File Shelf Implementation Report
Status:     Complete
Date:       2026-11-09
Baseline:   HEAD 1062c0d; Stage 12.2 and Stage 13.1 completed
Scope:      Reference-based file staging, URI validation, O_NOFOLLOW path authorization,
            identity & stale-reference detection, IPC protocol contracts
```

---

## 1. Executive Summary

Stage 13.2 implements KIVO's **File Shelf** backend subsystem (`kivo/shelf/`).

The File Shelf maintains transient desktop references to files, allowing users to stage files, inspect safe metadata, deduplicate entries, detect file replacement or modification on disk, and safely open files with `xdg-open`.

Staging a file:
- **Never copies** file contents (0 MB duplicated).
- **Never executes** the file directly.
- **Never reads** the full file content into RAM.
- **Never grants** directory or neighboring file permissions to LLM agents.
- **Never persists** references across restarts (in-memory only).

---

## 2. Implementation Deliverables

| File | Status | Description |
|---|---|---|
| `kivo/shelf/__init__.py` | **New** | Package exports: models, path policy, service. |
| `kivo/shelf/models.py` | **New** | Immutable data models: `ShelfItem` (sanitized public metadata with internal fields withheld), `FileIdentity` (device, inode, size, mtime, mode), filename sanitization, MIME inference. |
| `kivo/shelf/path_policy.py` | **New** | `parse_and_validate_file_uri`: strict URI validation (`file://` only, rejects remote authorities, NUL bytes, bidi overrides, control characters, double-encoded traversal). `PathAuthorizer`: descriptor-relative `O_NOFOLLOW` traversal, symlink rejection, regular file enforcement, system and sensitive path denial. |
| `kivo/shelf/service.py` | **New** | `FileShelfService`: in-memory reference staging (max 50 items, max 20 URIs per batch), deduplication, freshness check, `xdg-open` launcher, lifecycle. |
| `kivo/ipc/protocol.py` | **Modified** | Added 5 shelf operations and 1 event to allowlists; added strict parameter validators (`_validate_shelf_*`). |
| `kivo/ipc/handlers.py` | **Modified** | Injected `shelf_service`; implemented handlers for `shelf_list_items`, `shelf_add_uris`, `shelf_remove_item`, `shelf_clear`, `shelf_open_item`. |
| `kivo/app.py` | **Modified** | Initialized `FileShelfService` in lifecycle; connected `on_items_changed` to `shelf_items_changed`; added orderly shutdown cleanup. |
| `tests/test_shelf_policy.py` | **New** | 22 unit & adversarial tests for URI parsing, scheme rejection, bidi overrides, symlinks, system paths, and identity replacement. |
| `tests/test_shelf_service.py` | **New** | 11 tests for batch limits, shelf capacity (50), deduplication, stale file detection, disk file preservation on remove/clear, and `xdg-open` launch. |
| `tests/test_shelf_ipc.py` | **New** | 12 integration tests for protocol validation, parameter rejection, and event delivery. |
| `tests/test_stage12_2_regression.py` | **Modified** | Added regression assertions verifying zero shelf agent tools and zero shell execution in `kivo/shelf/`. |
| `docs/reverse-engineering/STAGE_13_2_FILE_SHELF_IMPLEMENTATION_REPORT.md` | **New** | Technical report and verification documentation. |

---

## 3. Filesystem Security Architecture Reused

### 3.1 Reuse of KIVO Security Foundations
Rather than creating an ad-hoc path validator, `PathAuthorizer` reuses and strictly adheres to KIVO's proven descriptor-based security model:
1. **Descriptor-Relative Traversal**: Path components are walked one by one using `O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC` directory file descriptors.
2. **Symlink Rejection**: Both intermediate directory components and the final target file are opened with `O_NOFOLLOW`. Symlinks are strictly refused with `symlink_not_allowed`.
3. **Regular Files Only**: Verified via `stat.S_ISREG`. Directories, FIFOs, sockets, character devices, and block devices are rejected with `unsupported_file_type`.
4. **Sensitive Path Heuristics**: Reuses `_is_sensitive_component` from `kivo/files/service.py`, denying access to `.ssh`, `.gnupg`, `.env`, private keys, and credential stores.
5. **System Prefix Denials**: Explicitly forbids paths in `/etc`, `/proc`, `/sys`, `/dev`, `/root`, `/var/run`, `/var/log`.
6. **TOCTOU Protection**: Compares `(st_dev, st_ino)` from `os.stat(follow_symlinks=False)` with `os.fstat(fd)` after opening to eliminate race conditions.

### 3.2 External Files Policy
- **Configured Workspace Files**: Files located under approved workspace roots configured in `$XDG_CONFIG_HOME/kivo/workspaces.json` are verified against the workspace root and flagged with `is_workspace=True`.
- **Explicitly Selected External Files**:
  - Files outside configured workspaces (e.g. dragged from user's `~/Downloads` or `~/Documents`) are supported **only if they are accessible regular files that pass descriptor-relative O_NOFOLLOW traversal and sensitive path heuristics**.
  - Staging an external file stores a reference for desktop `xdg-open` opening only.
  - **Crucial Boundary**: Staging an external file **does NOT grant LLMs, agent tools, or FileIntelligenceService permission to read, search, or index the parent directory**.

### 3.3 Copy / Move Mutation Operations: Deferred
- **Inspection Finding**: `MutationService` in `kivo/files/mutation_service.py` currently implements only atomic `create_text_file` with `RENAME_NOREPLACE` within mutation-enabled workspace roots. It does NOT provide a general-purpose file copy or move mechanism.
- **Decision**: In strict compliance with Section 2 & 10 of the prompt, **arbitrary copy/move operations are deliberately deferred**. We did NOT use insecure bypasses like `shutil.copy` or `shutil.move`. Staged files can be opened, un-staged, or cleared. Workspace transfers can be introduced in a future stage when a dedicated atomic copy/move mutation workflow is formally reviewed.

---

## 4. URI Parsing & Validation

Incoming URIs from future QML `DropArea` components are parsed and sanitized strictly:
- **Scheme**: Must strictly equal `"file"`. `http://`, `ftp://`, `smb://`, `custom://` are rejected with `unsupported_scheme`.
- **Authority**: Netloc must be empty (`file:///path`) or `"localhost"`. Remote network shares are rejected with `remote_authority_rejected`.
- **Double-Encoding Traversal**: Decoded exactly once with `urllib.parse.unquote`. Multi-encoded paths attempting `%252e%252e` traversal are detected and rejected with `traversal_detected`.
- **Character Filtering**: Paths with NUL bytes (`\x00`), C0/C1 control characters, or Unicode bidi override characters (`U+202A`–`U+202E`, `U+2066`–`U+2069`) are rejected with `invalid_uri`.
- **Batch & Size Caps**:
  - Max 20 URIs per `shelf_add_uris` request (enforced at protocol layer).
  - Max 4096 bytes per URI.
  - Max 50 items staged on shelf concurrently (enforced with `shelf_full`).

---

## 5. Identity & Stale-Reference Detection

At staging time, `FileIdentity` records:
- Device ID (`st_dev`)
- Inode number (`st_ino`)
- File size (`st_size`)
- Modification timestamp (`st_mtime_ns`)
- File mode (`st_mode`)

### Before Opening (`open_item`):
1. Safely re-opens the target path using descriptor-relative `O_NOFOLLOW` traversal.
2. Compares `(current.dev, current.ino)` with `(staged.dev, staged.ino)`.
3. If inode or device changed (file was replaced or recreated on disk):
   - Refuses execution.
   - Marks item `stale=True, availability="unavailable"`.
   - Returns typed error `stale_reference`.
4. If file was deleted or permissions revoked:
   - Returns typed error `stale_reference` / `file_not_found`.
5. If file is verified identical:
   - Invokes `run_bounded(["xdg-open", canonical_path], timeout=2.0)`.

---

## 6. IPC Protocol & Handler Contracts

### 6.1 Operations

| Operation | Dotted Alias | Parameters | Result | Notes |
|---|---|---|---|---|
| `shelf_list_items` | `shelf.list_items` | None or `{}` | `{"items": [...]}` | Sanitized metadata only (`id`, `name`, `file_type`, `size_bytes`, `staged_at`, `stale`, `availability`). Absolute paths and inodes withheld. |
| `shelf_add_uris` | `shelf.add_uris` | `{"uris": [str, ...]}` | `{"added": [...], "rejected": [...]}` | Batch capped at 20. Partially successful batches supported. |
| `shelf_remove_item` | `shelf.remove_item` | `{"id": str}` | `{"ok": bool}` | Removes reference from memory. Disk file is untouched. |
| `shelf_clear` | `shelf.clear` | None or `{}` | `{"ok": bool}` | Clears all references. Disk files are untouched. |
| `shelf_open_item` | `shelf.open_item` | `{"id": str}` | `{"ok": bool, "error_code": str, "error_message": str}` | Revalidates identity; launches `xdg-open`. Stale files rejected. |

### 6.2 Event

- `shelf_items_changed` (`shelf.items_changed`): Emitted whenever items are added, removed, or cleared. Payload: `{"count": int}`.

---

## 7. Verification Gates

All 10 verification gates passed cleanly:

| # | Gate | Command | Result |
|---|---|---|---|
| 1 | Focused File Shelf Tests | `.venv/bin/python -m pytest tests/test_shelf_*.py -v` | **45 passed** in 4.31s |
| 2 | Regression & Security Tests | `.venv/bin/python -m pytest tests/test_stage12_2_regression.py -v` | **10 passed** in 0.67s |
| 3 | Combined Stage 12 + 13 Focused Suite | `.venv/bin/python -m pytest tests/test_telemetry_*.py tests/test_microphone_*.py tests/test_clipboard_*.py tests/test_shelf_*.py tests/test_stage12_2_regression.py -q` | **230 passed** in 26.9s |
| 4 | Full Backend Test Suite | `.venv/bin/python -m pytest tests/ -q` | **852 passed, 176 subtests passed** in 69.4s (0 failures) |
| 5 | Shell Test Suite | `.venv/bin/python -m pytest shell/tests/ -q` | **16 passed** in 15.1s |
| 6 | Qt6 QML Lint | `/usr/lib/qt6/bin/qmllint shell/**/*.qml` | **Exit 0** (zero errors) |
| 7 | Desktop Health Check | `./scripts/desktop-health.sh` | **9/9 PASS** |
| 8 | Git Whitespace & Syntax | `git diff --check` | **Exit 0** |
| 9 | Python Compilation | `py_compile` all modified/added files | **ALL COMPILED OK** |
| 10 | Live Environment State | Verified host state | **Untouched** (no files created/deleted on host, mic unmuted) |

---

## 8. Scope Confirmation

- ✅ **Backend and IPC only**: No QML UI or Dynamic Notch modifications made (scheduled for Stage 13.4).
- ✅ **No Phone Bridge**: Retained for Stage 13.3.
- ✅ **No Agent Tools**: File Shelf operations are strictly for user desktop UI; never registered as LLM agent tools.
- ✅ **No File Deletions**: Removing or clearing shelf items never deletes user files on disk.
- ✅ **No Arbitrary Copy/Move**: Deferred until a formally reviewed atomic mutation workflow exists.
- ✅ **No git commit or push**.

---

**Verdict: PASS**
