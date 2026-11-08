"""
core/action_policy.py — central safety policy for all actions.

THE PROBLEM
    The codebase has 17 tool endpoints with ~100 distinct action verbs. Only 3
    actions on 1 tool (computer_settings: shutdown, restart, toggle_wifi) are
    behind the human confirmation gate. Meanwhile code_helper can run arbitrary
    shell commands, desktop.py exec()'s AI code, browser_control can click on
    authenticated banking sessions, and file_controller can delete files — all
    without any safety check.

    Worse, each action currently decides its own policy (most decide "none"),
    which means adding a new action requires the author to remember to add
    safety — and they won't.

THE DESIGN
    Every (tool, action) pair has a **risk level** and a **control**:

      RISK       MEANING                            DEFAULT CONTROL
      ──────────────────────────────────────────────────────────────
      SAFE       Read-only, no mutation              LOG
      LOW        Mutations with easy undo            UNDO
      MEDIUM     Side effects (messages, opens)      LOG
      HIGH       System changes, code execution      CONFIRM
      CRITICAL   Irreversible damage possible        CONFIRM (+ flag)

    Controls:
      LOG        Record the action for audit but let it run immediately.
      UNDO       Run immediately, push an undo entry (action must support it).
      CONFIRM    Gate behind the UI confirmation banner (core/confirm.py).
      DENY       Refuse to execute (reserved for dynamically disabled actions).

    The policy check runs inside ActionRegistry.run() — the single dispatch
    choke point — so no action can bypass it.

OVERRIDES
    The user can loosen or tighten policy at runtime or in config:
      - Per-tool:   policy.override("code_helper", Control.DENY)
      - Per-action: policy.override("code_helper", Control.CONFIRM, action="terminal")
      - Temporary:  policy.trust_session(True) makes HIGH → LOG (for testing)

AUDIT LOG
    Every tool invocation is logged to an in-memory ring buffer (1000 entries)
    with timestamp, tool, action, parameters, risk, control, and outcome.
    This is the foundation for replay, debugging, and anomaly detection.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Optional


# ── Enums ──────────────────────────────────────────────────────────────────

class Risk(Enum):
    SAFE     = 0   # read-only
    LOW      = 1   # minor, undoable mutations
    MEDIUM   = 2   # side effects (send message, open URL)
    HIGH     = 3   # system changes, code execution
    CRITICAL = 4   # irreversible damage possible

class Control(Enum):
    LOG     = auto()   # record and run
    UNDO    = auto()   # run and push undo entry
    CONFIRM = auto()   # gate behind human confirmation
    DENY    = auto()   # refuse to execute


# ── Audit log ──────────────────────────────────────────────────────────────

@dataclass
class AuditEntry:
    timestamp: float
    tool: str
    action: str
    parameters: dict
    risk: Risk
    control: Control
    outcome: str            # "executed", "confirmed", "denied", "error"
    result_snippet: str     # first 200 chars of result
    duration: float = 0.0
    # Shared agent authorization metadata. Empty for legacy-only callers.
    turn_id: str = ""
    tool_call_id: str = ""
    decision: str = ""


_AUDIT_MAX = 1000
_audit_log: list[AuditEntry] = []
_audit_lock = threading.Lock()


def _audit(entry: AuditEntry) -> None:
    with _audit_lock:
        _audit_log.append(entry)
        while len(_audit_log) > _AUDIT_MAX:
            _audit_log.pop(0)


def get_audit_log(limit: int = 50) -> list[AuditEntry]:
    """Most recent entries first."""
    with _audit_lock:
        return list(reversed(_audit_log[-limit:]))


def clear_audit_log() -> None:
    with _audit_lock:
        _audit_log.clear()


# ── Policy table ───────────────────────────────────────────────────────────
# (tool_name, action_verb) → (Risk, Control)
# Wildcard action "*" applies to all unmapped actions of that tool.

_POLICY: dict[tuple[str, str], tuple[Risk, Control]] = {
    # ── code_helper ──
    ("code_helper", "terminal"):    (Risk.CRITICAL, Control.CONFIRM),
    ("code_helper", "command"):     (Risk.CRITICAL, Control.CONFIRM),
    ("code_helper", "bash"):        (Risk.CRITICAL, Control.CONFIRM),
    ("code_helper", "execute"):     (Risk.CRITICAL, Control.CONFIRM),
    ("code_helper", "shell"):       (Risk.CRITICAL, Control.CONFIRM),
    ("code_helper", "run"):         (Risk.HIGH,     Control.CONFIRM),
    ("code_helper", "build"):       (Risk.MEDIUM,   Control.LOG),
    ("code_helper", "write"):       (Risk.LOW,      Control.UNDO),
    ("code_helper", "edit"):        (Risk.LOW,      Control.UNDO),
    ("code_helper", "explain"):     (Risk.SAFE,     Control.LOG),
    ("code_helper", "auto"):        (Risk.HIGH,     Control.CONFIRM),

    # ── computer_settings ──
    ("computer_settings", "shutdown"):     (Risk.CRITICAL, Control.CONFIRM),
    ("computer_settings", "restart"):      (Risk.CRITICAL, Control.CONFIRM),
    ("computer_settings", "toggle_wifi"):  (Risk.HIGH,     Control.CONFIRM),
    ("computer_settings", "type_text"):    (Risk.HIGH,     Control.LOG),
    ("computer_settings", "press_key"):    (Risk.HIGH,     Control.LOG),
    ("computer_settings", "*"):            (Risk.LOW,      Control.UNDO),

    # ── computer_control ──
    ("computer_control", "type"):          (Risk.HIGH,     Control.LOG),
    ("computer_control", "smart_type"):    (Risk.HIGH,     Control.LOG),
    ("computer_control", "hotkey"):        (Risk.MEDIUM,   Control.LOG),
    ("computer_control", "press"):         (Risk.MEDIUM,   Control.LOG),
    ("computer_control", "screenshot"):    (Risk.SAFE,     Control.LOG),
    ("computer_control", "user_data"):     (Risk.MEDIUM,   Control.LOG),
    ("computer_control", "*"):             (Risk.MEDIUM,   Control.LOG),

    # ── desktop_control ──
    ("desktop_control", "task"):           (Risk.CRITICAL, Control.CONFIRM),
    ("desktop_control", "organize"):       (Risk.HIGH,     Control.CONFIRM),
    ("desktop_control", "clean"):          (Risk.HIGH,     Control.CONFIRM),
    ("desktop_control", "wallpaper"):      (Risk.LOW,      Control.UNDO),
    ("desktop_control", "wallpaper_url"):  (Risk.LOW,      Control.UNDO),
    ("desktop_control", "list"):           (Risk.SAFE,     Control.LOG),
    ("desktop_control", "stats"):          (Risk.SAFE,     Control.LOG),

    # ── desktop_context ──
    ("desktop_context", "focus"):          (Risk.LOW,      Control.LOG),
    ("desktop_context", "focus_window"):   (Risk.LOW,      Control.LOG),
    ("desktop_context", "type_text"):      (Risk.MEDIUM,   Control.LOG),
    ("desktop_context", "press_key"):      (Risk.LOW,      Control.LOG),
    ("desktop_context", "screen_text"):    (Risk.SAFE,     Control.LOG),
    ("desktop_context", "pause_monitor"):  (Risk.LOW,      Control.LOG),
    ("desktop_context", "resume_monitor"): (Risk.LOW,      Control.LOG),
    ("desktop_context", "*"):              (Risk.SAFE,     Control.LOG),

    # ── file_controller ──
    ("file_controller", "delete"):         (Risk.HIGH,     Control.CONFIRM),
    ("file_controller", "move"):           (Risk.MEDIUM,   Control.UNDO),
    ("file_controller", "rename"):         (Risk.MEDIUM,   Control.UNDO),
    ("file_controller", "write"):          (Risk.MEDIUM,   Control.UNDO),
    ("file_controller", "create_file"):    (Risk.LOW,      Control.UNDO),
    ("file_controller", "create_folder"):  (Risk.LOW,      Control.UNDO),
    ("file_controller", "copy"):           (Risk.LOW,      Control.LOG),
    ("file_controller", "open"):           (Risk.LOW,      Control.LOG),
    ("file_controller", "*"):              (Risk.SAFE,     Control.LOG),

    # ── file_processor ──
    ("file_processor", "run"):             (Risk.HIGH,     Control.CONFIRM),
    ("file_processor", "*"):               (Risk.LOW,      Control.LOG),

    # ── orchestrate_task ──
    ("orchestrate_task", "*"):             (Risk.MEDIUM,   Control.LOG),

    # ── terminal_control (legacy execution blocked; use run_readonly_command) ──
    ("terminal_control", "execute_bg"):    (Risk.CRITICAL, Control.DENY),
    ("terminal_control", "subprocess"):    (Risk.CRITICAL, Control.DENY),
    ("terminal_control", "exec"):          (Risk.CRITICAL, Control.DENY),
    ("terminal_control", "type"):          (Risk.CRITICAL, Control.DENY),
    ("terminal_control", "type_command"):  (Risk.CRITICAL, Control.DENY),
    ("terminal_control", "run"):           (Risk.CRITICAL, Control.DENY),
    ("terminal_control", "run_and_read"):  (Risk.CRITICAL, Control.DENY),
    ("terminal_control", "read_output"):   (Risk.SAFE,     Control.LOG),
    ("terminal_control", "read"):          (Risk.SAFE,     Control.LOG),
    ("terminal_control", "read_screen"):   (Risk.SAFE,     Control.LOG),
    ("terminal_control", "status"):        (Risk.SAFE,     Control.LOG),
    ("terminal_control", "inspect"):       (Risk.SAFE,     Control.LOG),
    ("terminal_control", "list"):          (Risk.SAFE,     Control.LOG),
    ("terminal_control", "list_terminals"): (Risk.SAFE,    Control.LOG),
    ("terminal_control", "focus"):         (Risk.SAFE,     Control.LOG),
    ("terminal_control", "focus_terminal"): (Risk.SAFE,    Control.LOG),
    ("terminal_control", "open"):          (Risk.LOW,      Control.LOG),
    ("terminal_control", "*"):             (Risk.CRITICAL, Control.DENY),

    # ── run_readonly_command (Stage 10 controlled terminal access) ──
    ("run_readonly_command", "*"):         (Risk.MEDIUM,   Control.CONFIRM),

    # ── voice_biometrics ──
    ("voice_biometrics", "disable"):       (Risk.HIGH,     Control.CONFIRM),
    ("voice_biometrics", "*"):             (Risk.LOW,      Control.LOG),

    # ── open_app ──
    ("open_app", "*"):                     (Risk.MEDIUM,   Control.LOG),

    # ── browser_control ──
    ("browser_control", "fill_form"):      (Risk.HIGH,     Control.CONFIRM),
    ("browser_control", "smart_click"):    (Risk.MEDIUM,   Control.LOG),
    ("browser_control", "smart_type"):     (Risk.MEDIUM,   Control.LOG),
    ("browser_control", "type_desktop"):   (Risk.MEDIUM,   Control.LOG),
    ("browser_control", "read_page"):      (Risk.SAFE,     Control.LOG),
    ("browser_control", "close"):          (Risk.MEDIUM,   Control.LOG),
    ("browser_control", "close_all"):      (Risk.HIGH,     Control.CONFIRM),
    ("browser_control", "*"):              (Risk.LOW,      Control.LOG),

    # ── send_message ──
    ("send_message", "*"):                 (Risk.HIGH,     Control.CONFIRM),

    # ── dev_agent ──
    ("dev_agent", "*"):                    (Risk.HIGH,     Control.CONFIRM),

    # ── game_updater ──
    ("game_updater", "install"):           (Risk.MEDIUM,   Control.CONFIRM),
    ("game_updater", "update"):            (Risk.MEDIUM,   Control.LOG),
    ("game_updater", "*"):                 (Risk.LOW,      Control.LOG),

    # ── reminder ──
    ("reminder", "*"):                     (Risk.MEDIUM,   Control.LOG),

    # ── LOW-risk read-only tools ──
    ("web_search", "*"):                   (Risk.SAFE,     Control.LOG),
    ("weather_report", "*"):               (Risk.SAFE,     Control.LOG),
    ("youtube_video", "*"):                (Risk.LOW,      Control.LOG),
    ("video_player", "*"):                 (Risk.LOW,      Control.LOG),
    ("flight_finder", "*"):                (Risk.SAFE,     Control.LOG),
}

# Default for any tool/action not in the table
_DEFAULT_POLICY = (Risk.MEDIUM, Control.LOG)

_policy_lock = threading.RLock()


# ── Overrides ──────────────────────────────────────────────────────────────

_overrides: dict[tuple[str, str], tuple[Risk, Control]] = {}
_trust_session: bool = False

# ── Scoped grants ──────────────────────────────────────────────────────────
#
# A "grant" is a narrow, revocable loosening. Unlike trust_session (which opens
# everything) and unlike an override (which is permanent until cleared), a
# grant expires after `max_uses` invocations or `ttl_seconds`, whichever comes
# first. This is what the permission service issues when the user says "go
# ahead and delete files for the next five minutes".

@dataclass
class _Grant:
    tool: str
    action: str
    control: Control
    remaining: int          # max_uses; -1 = unlimited until ttl
    expires_at: float       # monotonic; 0 = no expiry

_grants: dict[tuple[str, str], _Grant] = {}
_grants_lock = threading.Lock()


def grant(
    tool: str,
    control: Control = Control.LOG,
    action: str = "*",
    *,
    max_uses: int = 1,
    ttl_seconds: float = 300.0,
) -> str:
    """Issue a scoped, revocable permission.

    Returns a human-readable confirmation for the UI.
    """
    now = time.monotonic()
    expires = (now + ttl_seconds) if ttl_seconds > 0 else 0.0
    with _grants_lock:
        _grants[(tool, action)] = _Grant(
            tool=tool, action=action, control=control,
            remaining=max_uses, expires_at=expires,
        )
    desc = f"{tool}:{action}"
    parts = []
    if max_uses > 0:
        parts.append(f"{max_uses} use{'s' if max_uses != 1 else ''}")
    if ttl_seconds > 0:
        parts.append(f"{int(ttl_seconds)}s")
    return f"Granted {desc} → {control.name} ({', '.join(parts)})"


def revoke(tool: str, action: str = "*") -> bool:
    with _grants_lock:
        return _grants.pop((tool, action), None) is not None


def revoke_all() -> int:
    with _grants_lock:
        n = len(_grants)
        _grants.clear()
    return n


def list_grants() -> list[dict]:
    now = time.monotonic()
    with _grants_lock:
        return [
            {
                "tool": g.tool,
                "action": g.action,
                "control": g.control.name,
                "remaining": g.remaining,
                "expires_in": max(0, g.expires_at - now) if g.expires_at else None,
            }
            for g in _grants.values()
            if (g.expires_at == 0 or g.expires_at > now) and g.remaining != 0
        ]


def _consume_grant(tool: str, action: str) -> tuple[Risk, Control] | None:
    """If a grant applies, consume one use and return its (risk, control).

    Expired or exhausted grants are cleaned up lazily.
    """
    now = time.monotonic()
    with _grants_lock:
        for key in ((tool, action), (tool, "*")):
            g = _grants.get(key)
            if g is None:
                continue
            if g.expires_at and g.expires_at < now:
                del _grants[key]
                continue
            if g.remaining == 0:
                del _grants[key]
                continue
            # Valid grant — consume it.
            if g.remaining > 0:
                g.remaining -= 1
            # Look up the base risk so the audit log is consistent.
            base_risk = _POLICY.get((tool, action), _POLICY.get((tool, "*"), _DEFAULT_POLICY))[0]
            return base_risk, g.control
    return None


def override(
    tool: str,
    control: Control,
    action: str = "*",
    risk: Risk | None = None,
) -> None:
    """
    Override the policy for a tool or specific action at runtime.
    Example: policy.override("code_helper", Control.DENY) blocks all code_helper actions.
    """
    with _policy_lock:
        base_risk = risk or lookup(tool, action)[0]
        _overrides[(tool, action)] = (base_risk, control)


def clear_override(tool: str, action: str = "*") -> None:
    with _policy_lock:
        _overrides.pop((tool, action), None)


def clear_all_overrides() -> None:
    with _policy_lock:
        _overrides.clear()


def trust_session(enabled: bool = True) -> None:
    """
    Temporarily loosen all CONFIRM policies to LOG (for testing/demo).
    Does NOT affect DENY or CRITICAL+CONFIRM overrides that were set explicitly.
    """
    global _trust_session
    _trust_session = enabled


# ── Lookup ─────────────────────────────────────────────────────────────────

def lookup(
    tool: str,
    action: str = "*",
    *,
    respect_trust: bool = True,
) -> tuple[Risk, Control]:
    """
    Get the (Risk, Control) for a specific (tool, action) pair.
    Resolution order:
      1. Explicit override for (tool, action)
      2. Explicit override for (tool, "*")
      3. Scoped grant for (tool, action) or (tool, "*")
      4. Static policy for (tool, action)
      5. Static policy for (tool, "*")
      6. _DEFAULT_POLICY

    ``respect_trust=False`` keeps CONFIRM controls in force even when the
    legacy ``trust_session`` demo switch is enabled. The shared agent
    authorization path always uses this mode.
    """
    with _policy_lock:
        # 1. Exact override
        if (tool, action) in _overrides:
            return _overrides[(tool, action)]
        # 2. Wildcard override
        if (tool, "*") in _overrides:
            return _overrides[(tool, "*")]

    # 3. Scoped grant (consumed on use)
    granted = _consume_grant(tool, action)
    if granted is not None:
        return granted

    # 4. Exact static policy
    entry = _POLICY.get((tool, action))
    if entry:
        risk, ctrl = entry
        return _maybe_trust(risk, ctrl, respect_trust)
    # 5. Wildcard static policy
    entry = _POLICY.get((tool, "*"))
    if entry:
        risk, ctrl = entry
        return _maybe_trust(risk, ctrl, respect_trust)
    # 6. Default
    risk, ctrl = _DEFAULT_POLICY
    return _maybe_trust(risk, ctrl, respect_trust)


def _maybe_trust(
    risk: Risk,
    ctrl: Control,
    respect_trust: bool = True,
) -> tuple[Risk, Control]:
    """If trust_session is on, downgrade CONFIRM → LOG for non-critical."""
    if respect_trust and _trust_session and ctrl == Control.CONFIRM and risk != Risk.CRITICAL:
        return risk, Control.LOG
    return risk, ctrl


# ── Enforcement ────────────────────────────────────────────────────────────

def check(
    tool: str,
    action: str,
    parameters: dict,
    *,
    respect_trust: bool = True,
    turn_id: str = "",
    tool_call_id: str = "",
) -> tuple[Risk, Control, str]:
    """
    Evaluate the policy for a tool call and return (risk, control, reason).

    reason is empty string if allowed, or a denial message if Control.DENY.
    This is called by ActionRegistry.run() before executing the handler.
    """
    risk, ctrl = lookup(tool, action, respect_trust=respect_trust)

    if ctrl == Control.DENY:
        reason = (
            f"Action '{action}' on tool '{tool}' is currently disabled "
            f"(risk level: {risk.name}). Ask the user to enable it if needed."
        )
        _audit(AuditEntry(
            timestamp=time.time(), tool=tool, action=action,
            parameters=_safe_params(parameters),
            risk=risk, control=ctrl,
            outcome="denied", result_snippet=reason,
            turn_id=turn_id, tool_call_id=tool_call_id, decision="deny",
        ))
        return risk, ctrl, reason

    return risk, ctrl, ""


def record_execution(
    tool: str,
    action: str,
    parameters: dict,
    risk: Risk,
    control: Control,
    outcome: str,
    result: str,
    duration: float = 0.0,
    *,
    turn_id: str = "",
    tool_call_id: str = "",
    decision: str = "",
) -> None:
    """Record a completed tool execution in the audit log."""
    _audit(AuditEntry(
        timestamp=time.time(), tool=tool, action=action,
        parameters=_safe_params(parameters),
        risk=risk, control=control,
        outcome=outcome, result_snippet=(result or "")[:200],
        duration=duration,
        turn_id=turn_id, tool_call_id=tool_call_id, decision=decision,
    ))


# ── Helpers ────────────────────────────────────────────────────────────────

def _safe_params(params: dict) -> dict:
    """Strip or truncate parameter values for audit storage."""
    safe = {}
    for k, v in (params or {}).items():
        s = str(v)
        safe[k] = s[:500] if len(s) > 500 else s
    return safe


def _extract_action(tool: str, parameters: dict) -> str:
    """
    Heuristic: most tools use 'action' as the routing key.
    Some use 'task' or 'request'. Falls back to "*".
    """
    for key in ("action", "task", "request", "mode"):
        val = parameters.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().lower()
    return "*"


def describe_policy(tool: str | None = None) -> str:
    """Human-readable summary of the current policy."""
    lines = ["Action Safety Policy:"]
    lines.append(f"  Trust session: {'ON (relaxed)' if _trust_session else 'OFF (normal)'}")
    lines.append(f"  Override count: {len(_overrides)}")
    active_grants = list_grants()
    lines.append(f"  Active grants: {len(active_grants)}")
    lines.append("")

    entries = _POLICY
    if tool:
        entries = {k: v for k, v in _POLICY.items() if k[0] == tool}

    current_tool = ""
    for (t, a), (risk, ctrl) in sorted(entries.items()):
        if t != current_tool:
            lines.append(f"  [{t}]")
            current_tool = t
        override_marker = ""
        if (t, a) in _overrides:
            _, oc = _overrides[(t, a)]
            override_marker = f"  (overridden → {oc.name})"
        lines.append(f"    {a:25s} {risk.name:10s} {ctrl.name}{override_marker}")

    if active_grants:
        lines.append("")
        lines.append("  Active grants:")
        for g in active_grants:
            exp = f"{g['expires_in']:.0f}s left" if g['expires_in'] is not None else "no expiry"
            lines.append(f"    {g['tool']}:{g['action']} → {g['control']} "
                        f"({g['remaining']} uses, {exp})")

    return "\n".join(lines)
