#!/usr/bin/env bash
# KIVO Stage 5 — Desktop integration installer / uninstaller.
#
# Installs the KIVO systemd user services and the niri keybinding snippet,
# registers the niri include and enables + starts the KIVO user services.
#
#   ./scripts/install-desktop-integration.sh            # install + enable + start
#   ./scripts/install-desktop-integration.sh --uninstall
#
# Safety properties:
#   - Never touches unrelated systemd user unit files.
#   - Never overwrites the complete niri configuration; only adds/removes the
#     KIVO-owned include line.
#   - Never deletes user data or the KIVO project itself.
#   - Does not require root privileges for the normal flow.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
UNIT_TEMPLATE_DIR="${PROJECT_ROOT}/systemd/user"
USER_UNIT_DIR="${HOME}/.config/systemd/user"
NIRI_DIR="${HOME}/.config/niri"
NIRI_SNIPPET="${NIRI_DIR}/kivo.kdl"
NIRI_CONFIG="${NIRI_DIR}/config.kdl"
BACKEND_UNIT="kivo-backend.service"
SHELL_UNIT="kivo-shell.service"

log() { printf '[install-desktop-integration] %s\n' "$*"; }
fail() { printf '[install-desktop-integration] ERROR: %s\n' "$*" >&2; exit 1; }

ACTION="install"
for arg in "$@"; do
    case "$arg" in
        --uninstall) ACTION="uninstall" ;;
        -h|--help)
            cat <<'USAGE'
Usage: ./scripts/install-desktop-integration.sh [--uninstall]

KIVO Stage 5 — Desktop integration installer / uninstaller.

Actions:
  install            (default) Install KIVO user systemd units and the niri
                     keybinding snippet, register the niri include, reload the
                     user systemd manager and enable + start the KIVO services.
  --uninstall        Stop + disable the KIVO user services, remove the installed
                     KIVO unit files and niri snippet, and remove the KIVO
                     include line from the niri config.

The installer never touches unrelated service files or the rest of the niri
configuration, and never deletes user data or the KIVO project itself.
USAGE
            exit 0
            ;;
        *)
            fail "unknown argument: ${arg} (supported: --uninstall, -h, --help)"
            ;;
    esac
done

# ── Safety: user services must run as the invoking (non-root) user ──────────
if [ "$(id -u)" -eq 0 ]; then
    fail "this script must not be run as root; user-level systemd services require the regular user session"
fi

# ── Prerequisite checks (install action) ────────────────────────────────────
if [ "$ACTION" = "install" ]; then
    log "checking prerequisites"
    command -v qs >/dev/null 2>&1 || fail "quickshell executable 'qs' not found in PATH"
    command -v niri >/dev/null 2>&1 || fail "niri executable not found in PATH"
    [ -x "${PROJECT_ROOT}/.venv/bin/python" ] || [ -x "${PROJECT_ROOT}/.venv/bin/python3" ] \
        || fail "python virtual environment not found at ${PROJECT_ROOT}/.venv"
    [ -f "${PROJECT_ROOT}/shell/shell.qml" ] || fail "Quickshell config not found: ${PROJECT_ROOT}/shell/shell.qml"
    [ -f "${UNIT_TEMPLATE_DIR}/${BACKEND_UNIT}" ] || fail "backend unit template missing: ${UNIT_TEMPLATE_DIR}/${BACKEND_UNIT}"
    [ -f "${UNIT_TEMPLATE_DIR}/${SHELL_UNIT}" ] || fail "shell unit template missing: ${UNIT_TEMPLATE_DIR}/${SHELL_UNIT}"
    log "prerequisites OK"
fi

# ── Install action ──────────────────────────────────────────────────────────
if [ "$ACTION" = "install" ]; then
    log "installing user systemd units"
    mkdir -p "${USER_UNIT_DIR}"
    for unit in "${BACKEND_UNIT}" "${SHELL_UNIT}"; do
        if [ -f "${USER_UNIT_DIR}/${unit}" ]; then
            cp "${USER_UNIT_DIR}/${unit}" "${USER_UNIT_DIR}/${unit}.kivo-backup"
            log "backed up existing ${unit} to ${USER_UNIT_DIR}/${unit}.kivo-backup"
        fi
        cp "${UNIT_TEMPLATE_DIR}/${unit}" "${USER_UNIT_DIR}/${unit}"
        chmod 644 "${USER_UNIT_DIR}/${unit}"
    done

    log "writing niri keybinding snippet"
    mkdir -p "${NIRI_DIR}"
    if [ -f "${NIRI_SNIPPET}" ]; then
        cp "${NIRI_SNIPPET}" "${NIRI_SNIPPET}.kivo-backup"
        log "backed up existing ${NIRI_SNIPPET} to ${NIRI_SNIPPET}.kivo-backup"
    fi
    cat > "${NIRI_SNIPPET}" <<EOF
// KIVO desktop integration keybindings for niri.
// Managed by scripts/install-desktop-integration.sh - do not edit by hand if you
// want the integration to survive a reinstall. Back up before editing.
binds {
    // Super+Alt+K - toggle the KIVO Dashboard.
    Mod+Alt+K { spawn "qs" "ipc" "-p" "${PROJECT_ROOT}/shell" "call" "kivo-notch" "toggleDashboard"; }
    // Super+Shift+Alt+K - toggle the KIVO Dynamic Notch.
    Mod+Shift+Alt+K { spawn "qs" "ipc" "-p" "${PROJECT_ROOT}/shell" "call" "kivo-notch" "toggle"; }
    // Super+Ctrl+Alt+K - restart the KIVO backend service.
    Mod+Ctrl+Alt+K { spawn "systemctl" "--user" "restart" "kivo-backend.service"; }
}
EOF
    chmod 644 "${NIRI_SNIPPET}"

    log "registering niri include (only when absent)"
    if ! grep -q 'kivo\.kdl' "${NIRI_CONFIG}" 2>/dev/null; then
        if [ -f "${NIRI_CONFIG}" ]; then
            if [ -n "$(tail -c 1 "${NIRI_CONFIG}" | tr -d '\n')" ]; then
                printf '\n' >> "${NIRI_CONFIG}"
            fi
        fi
        printf '// KIVO desktop integration (managed by scripts/install-desktop-integration.sh)\ninclude optional=true "kivo.kdl"\n' >> "${NIRI_CONFIG}"
        log "added include line to ${NIRI_CONFIG}"
    else
        log "include already present in ${NIRI_CONFIG}; left untouched"
    fi

    log "reloading user systemd manager"
    systemctl --user daemon-reload || fail "systemctl --user daemon-reload failed"

    log "enabling and starting KIVO user services"
    systemctl --user enable "${BACKEND_UNIT}" "${SHELL_UNIT}" \
        || fail "systemctl --user enable failed for KIVO services"
    systemctl --user start "${BACKEND_UNIT}" "${SHELL_UNIT}" \
        || fail "systemctl --user start failed for KIVO services"

    log "installation complete"
fi

# ── Uninstall action ─────────────────────────────────────────────────────────
if [ "$ACTION" = "uninstall" ]; then
    log "stopping and disabling KIVO user services"
    systemctl --user stop "${BACKEND_UNIT}" "${SHELL_UNIT}" >/dev/null 2>&1 || true
    systemctl --user disable "${BACKEND_UNIT}" "${SHELL_UNIT}" >/dev/null 2>&1 || true

    log "removing installed KIVO user unit files"
    rm -f "${USER_UNIT_DIR}/${BACKEND_UNIT}" "${USER_UNIT_DIR}/${SHELL_UNIT}"

    log "removing niri keybinding snippet"
    rm -f "${NIRI_SNIPPET}"

    log "removing niri include line"
    if [ -f "${NIRI_CONFIG}" ]; then
        sed -i '/kivo\.kdl/d' "${NIRI_CONFIG}"
    fi

    log "reloading user systemd manager"
    systemctl --user daemon-reload >/dev/null 2>&1 || true

    log "uninstall complete; user data and project files were not touched"
fi
