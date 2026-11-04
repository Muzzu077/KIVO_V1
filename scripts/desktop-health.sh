#!/usr/bin/env bash
# KIVO Stage 5 — Desktop health check.
#
# Reports PASS / FAIL / WARNING / NOT CONFIGURED for the KIVO desktop
# integration without ever printing credentials, voiceprints or memory content.
#
# Checks:
#   1. Backend systemd user service state
#   2. Shell (Quickshell) systemd user service state
#   3. Backend process availability
#   4. IPC socket existence and permissions
#   5. Actual IPC response (backend get_status)
#   6. Quickshell process availability
#   7. Wayland environment availability
#   8. Required project files
#   9. niri keybinding snippet installation state

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKEND_UNIT="kivo-backend.service"
SHELL_UNIT="kivo-shell.service"
NIRI_SNIPPET="${HOME}/.config/niri/kivo.kdl"

PASS=0
FAIL=0
WARNING=0
NOT_CONFIGURED=0

record() {
    # record <status> <label> <detail>
    local status="$1" label="$2" detail="$3"
    case "$status" in
        PASS) PASS=$((PASS + 1)) ;;
        FAIL) FAIL=$((FAIL + 1)) ;;
        WARNING) WARNING=$((WARNING + 1)) ;;
        NOT_CONFIGURED) NOT_CONFIGURED=$((NOT_CONFIGURED + 1)) ;;
    esac
    printf '%-10s %s\n' "$status" "$label: $detail"
}

backend_service_state() {
    systemctl --user is-active --quiet "${BACKEND_UNIT}" 2>/dev/null
}

shell_service_state() {
    systemctl --user is-active --quiet "${SHELL_UNIT}" 2>/dev/null
}

backend_running() {
    pgrep -f 'python.*-m kivo' >/dev/null 2>&1
}

shell_running() {
    pgrep -f 'qs.*-p .*KIVO_V1' >/dev/null 2>&1
}

socket_path() {
    local runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    printf '%s/kivo/kivo.sock' "${runtime_dir}"
}

ipc_responds() {
    # Sends a read-only get_status request over the local IPC socket.
    local sock
    sock="$(socket_path)"
    [ -S "${sock}" ] || return 2 # socket missing
    "${PROJECT_ROOT}/.venv/bin/python" - "${sock}" <<'PY' 2>/dev/null
import json, socket, sys

sock_path = sys.argv[1]
request = {
    "version": 1,
    "type": "request",
    "id": "desktop-health-probe",
    "operation": "get_status",
    "params": {},
}
client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.settimeout(5.0)
client.connect(sock_path)
client.sendall((json.dumps(request) + "\n").encode("utf-8"))
buffer = b""
while b"\n" not in buffer:
    chunk = client.recv(65536)
    if not chunk:
        break
    buffer += chunk
client.close()
line = buffer.decode("utf-8").strip()
if not line:
    print("no_response")
    sys.exit(1)
response = json.loads(line)
if response.get("ok"):
    sys.exit(0)
print("error=%s" % response.get("error", {}).get("message", "unknown"))
sys.exit(1)
PY
}

# ── 1. Backend service ───────────────────────────────────────────────────────
if backend_service_state; then
    record PASS "backend_service" "kivo-backend.service is active"
else
    state="$(systemctl --user is-active "${BACKEND_UNIT}" 2>/dev/null || echo unknown)"
    if [ "$state" = "inactive" ] || [ "$state" = "failed" ] || [ "$state" = "unknown" ]; then
        record FAIL "backend_service" "kivo-backend.service state=${state}"
    else
        record WARNING "backend_service" "kivo-backend.service state=${state} (not enabled?)"
    fi
fi

# ── 2. Shell service ─────────────────────────────────────────────────────────
if shell_service_state; then
    record PASS "shell_service" "kivo-shell.service is active"
else
    state="$(systemctl --user is-active "${SHELL_UNIT}" 2>/dev/null || echo unknown)"
    if [ "$state" = "inactive" ] || [ "$state" = "failed" ] || [ "$state" = "unknown" ]; then
        record FAIL "shell_service" "kivo-shell.service state=${state}"
    else
        record WARNING "shell_service" "kivo-shell.service state=${state} (not enabled?)"
    fi
fi

# ── 3. Backend process ───────────────────────────────────────────────────────
if backend_running; then
    record PASS "backend_process" "backend process found"
else
    record FAIL "backend_process" "no 'python -m kivo' process running"
fi

# ── 4. IPC socket ────────────────────────────────────────────────────────────
sock="$(socket_path)"
if [ -S "${sock}" ]; then
    record PASS "ipc_socket" "socket present: ${sock}"
else
    record FAIL "ipc_socket" "socket missing: ${sock}"
fi

# ── 5. IPC response ──────────────────────────────────────────────────────────
if [ -S "${sock}" ]; then
    if ipc_responds; then
        record PASS "ipc_response" "backend get_status responded ok"
    else
        record FAIL "ipc_response" "backend IPC get_status failed or timed out"
    fi
else
    record FAIL "ipc_response" "skipped (IPC socket not present)"
fi

# ── 6. Quickshell process ────────────────────────────────────────────────────
if shell_running; then
    record PASS "shell_process" "quickshell KIVO process found"
else
    record FAIL "shell_process" "no Quickshell KIVO process found"
fi

# ── 7. Wayland environment ───────────────────────────────────────────────────
if [ -n "${WAYLAND_DISPLAY:-}" ] && [ -n "${XDG_RUNTIME_DIR:-}" ]; then
    record PASS "wayland_env" "WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-} XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-}"
else
    record FAIL "wayland_env" "WAYLAND_DISPLAY/XDG_RUNTIME_DIR not set; not running inside a Wayland session"
fi

# ── 8. Required project files ─────────────────────────────────────────────────
if [ -f "${PROJECT_ROOT}/kivo/__main__.py" ] && [ -f "${PROJECT_ROOT}/shell/shell.qml" ] && [ -x "${PROJECT_ROOT}/.venv/bin/python" ]; then
    record PASS "project_files" "backend entry + shell QML + venv present"
else
    record FAIL "project_files" "required KIVO project files missing"
fi

# ── 9. niri snippet ───────────────────────────────────────────────────────────
if [ -f "${NIRI_SNIPPET}" ]; then
    record PASS "niri_snippet" "kivo.niri snippet installed"
else
    record NOT_CONFIGURED "niri_snippet" "kivo.niri snippet not installed"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
printf '\n=== KIVO desktop health check summary ===\n'
printf 'PASS: %d  FAIL: %d  WARNING: %d  NOT CONFIGURED: %d\n' "${PASS}" "${FAIL}" "${WARNING}" "${NOT_CONFIGURED}"

if [ "${FAIL}" -gt 0 ]; then
    printf 'RESULT: FAIL\n'
    exit 1
fi
printf 'RESULT: PASS\n'
exit 0
