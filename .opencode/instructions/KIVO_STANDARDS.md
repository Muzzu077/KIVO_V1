# KIVO Engineering Standards & Architectural Boundaries

This document defines the architectural constraints, development rules, and quality gates for the **KIVO** desktop assistant. All agentic workflows (planning, TDD, code review, security analysis) must comply with these standards.

---

## 1. System & Runtime Architecture

- **Operating System**: Arch Linux (Wayland only, niri 26.04 scrollable-tiling compositor).
- **Backend**: Python 3.14 running in dedicated virtual environment (`.venv`).
  - Entry point: `python -m kivo` (`kivo/app.py`).
  - Lifecycle: `initialize()` -> `run()` -> `shutdown()`. Orders component release and unlinks IPC socket.
- **Frontend**: Quickshell 0.3.2 (Qt6 / QML / Wayland layer-shell).
  - Launch: `qs -n -p shell`.
  - Architecture: `ShellRoot` hosting `DynamicNotch` (top-center layer surface) and floating `KivoDashboard`.
  - Shared IPC: Single `KivoIpc` bridge scope; UI components never open second backend connections.
- **IPC Protocol**:
  - Unix-domain stream socket at `$XDG_RUNTIME_DIR/kivo/kivo.sock`.
  - Framing: Versioned newline-delimited JSON v1 (`\n`). Max message frame: 64 KB (`MAX_MESSAGE_BYTES`).
  - Permissions: Socket mode `0600`, parent directory mode `0700`.
  - Safety: Stale socket recovery only after failed connect probe. Never overwrite non-socket files.
  - Zero raw data: Audio bytes, images, and credentials must **never** be transmitted over IPC.
- **Desktop Services**:
  - `systemd --user` units: `kivo-backend.service` and `kivo-shell.service`.
  - Managed via `scripts/install-desktop-integration.sh`.
  - Diagnostics: `scripts/desktop-health.sh` (9 probes).

---

## 2. Non-Negotiable Development Rules

1. **Inspect First**: Always inspect the repository and verify installed capabilities before making changes.
2. **Preserve Existing Stages**: Stages 1 through 6 are completed and verified on actual hardware. Never break existing functionality.
3. **No Architecture Rewrites**: Do not redesign backend, IPC, Dynamic Notch, or Dashboard.
4. **No Hardcoded User Paths**: Never hardcode `/home/username`, user UID, or machine-specific directories in version-controlled code. Use XDG paths (`XDG_RUNTIME_DIR`, `XDG_CONFIG_HOME`, Path.home()).
5. **No Second Backends / Shells**: Never introduce duplicate processes. Use `qs -n` and socket locks.
6. **No Mock or Fabricated Data in Production**: Never simulate successes or fabricate UI status. If hardware/service is unavailable, display accurate unavailable state.
7. **Privacy & Explicit Action**:
   - Voice capture runs **only** during explicit Push-to-Talk (PTT).
   - No continuous listening, background recording, or hidden mic activation.
   - Bounded in-memory buffering (max 120s cutoff); clear buffers on shutdown.
8. **Git Safety**:
   - Never push to GitHub (`git push` prohibited).
   - Verify `git diff --check` before committing.
   - All commits must be verified locally with passing tests.

---

## 3. Repeatable Engineering Workflow

Every feature, bugfix, or refactoring task must follow the 5-phase loop:

```
Plan → TDD (Red) → Implement (Green) → Verify (Gate) → Review & Commit
```

### Phase 1: Planning
- Use structured planning (`agents/planner.md`).
- Document acceptance criteria, affected files, and test plan.
- Establish strict boundaries against scope creep.

### Phase 2: TDD (Red)
- Reference `skills/tdd-workflow/SKILL.md` and `skills/python-testing/SKILL.md`.
- Write failing unit/integration tests in `tests/` (for backend) or `shell/tests/` (for UI).
- Run `pytest` to verify tests fail for the intended reason.

### Phase 3: Implement (Green)
- Reference `skills/python-patterns/SKILL.md`.
- Implement the minimum code needed to pass the tests.
- Use explicit typing, frozen dataclasses for immutable payloads, and bounded collections.

### Phase 4: Verification Gate
All 5 checks must pass before proceeding:
1. Backend test suite: `.venv/bin/python -m pytest tests/ -q`
2. Shell test suite: `.venv/bin/python -m pytest shell/tests/ -q`
3. Qt6 QML lint: `/usr/lib/qt6/bin/qmllint shell/...` (zero errors)
4. Desktop health diagnostic: `./scripts/desktop-health.sh` (9/9 PASS)
5. Whitespace and syntax check: `git diff --check`

### Phase 5: Security Review & Local Commit
- Reference `skills/security-review/SKILL.md`.
- Verify no secrets, credentials, or raw audio bytes leak into logs or IPC payloads.
- Commit locally with a descriptive message. Do not push.
