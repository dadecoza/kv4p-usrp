#!/usr/bin/env bash
set -Eeuo pipefail

readonly REPO_URL="https://github.com/dadecoza/kv4p-usrp.git"
readonly INSTALL_DIR="/opt/kv4p-usrp"
readonly CONFIG_FILE="/etc/default/kv4p-usrp"
readonly SERVICE_FILE="/etc/systemd/system/kv4p-usrp.service"
readonly SERVICE_USER="kv4p-usrp"

DEVICE=""
RX_FREQUENCY=""
TX_FREQUENCY=""
SQUELCH="4"
POWER="high"
RX_GAIN="1.0"
BANDWIDTH="keep"
PTT_HANG="0.40"
ALLSTAR_HOST="127.0.0.1"
RECEIVE_ONLY=""
NO_START=0
UNINSTALL=0
RECONFIGURE=0
CONFIG_REQUESTED=0

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Install or update kv4p-usrp as a systemd service.

Usage: sudo bash install.sh [options]
  --device PATH          KV4P serial device (prefer /dev/serial/by-id/...)
  --rx-frequency MHz     Radio receive frequency (required for new installs)
  --tx-frequency MHz     Radio transmit frequency (required for new installs)
  --squelch 0..8         Squelch level (default: 4)
  --rx-gain FACTOR       RF-to-AllStar audio multiplier (default: 1.0)
  --bandwidth MODE       wide, narrow, or keep (default: keep device setting)
  --power high|low       Transmit power (default: high)
  --ptt-hang SECONDS     PTT hang time (default: 0.40)
  --allstar-host HOST    AllStar host (default: 127.0.0.1)
  --receive-only         Disable RF transmission (interactive default)
  --allow-transmit       Permit RF transmission
  --reconfigure          Replace the existing service configuration
  --no-start             Install but do not enable/start the service
  --uninstall            Remove the service and program; preserve configuration
  -h, --help             Show this help

Example:
  curl -fsSL https://raw.githubusercontent.com/dadecoza/kv4p-usrp/main/install.sh \
    | sudo bash
EOF
}

need_value() { [[ $# -ge 2 ]] || die "$1 requires a value"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --device) need_value "$@"; DEVICE=$2; CONFIG_REQUESTED=1; shift 2 ;;
        --rx-frequency) need_value "$@"; RX_FREQUENCY=$2; CONFIG_REQUESTED=1; shift 2 ;;
        --tx-frequency) need_value "$@"; TX_FREQUENCY=$2; CONFIG_REQUESTED=1; shift 2 ;;
        --squelch) need_value "$@"; SQUELCH=$2; CONFIG_REQUESTED=1; shift 2 ;;
        --rx-gain) need_value "$@"; RX_GAIN=$2; CONFIG_REQUESTED=1; shift 2 ;;
        --bandwidth) need_value "$@"; BANDWIDTH=$2; CONFIG_REQUESTED=1; shift 2 ;;
        --power) need_value "$@"; POWER=$2; CONFIG_REQUESTED=1; shift 2 ;;
        --ptt-hang) need_value "$@"; PTT_HANG=$2; CONFIG_REQUESTED=1; shift 2 ;;
        --allstar-host) need_value "$@"; ALLSTAR_HOST=$2; CONFIG_REQUESTED=1; shift 2 ;;
        --receive-only) RECEIVE_ONLY=1; CONFIG_REQUESTED=1; shift ;;
        --allow-transmit) RECEIVE_ONLY=0; CONFIG_REQUESTED=1; shift ;;
        --reconfigure) RECONFIGURE=1; shift ;;
        --no-start) NO_START=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "run this installer as root (for example: curl ... | sudo bash)"
command -v systemctl >/dev/null || die "systemd is required"

if [[ $UNINSTALL -eq 1 ]]; then
    log "Removing kv4p-usrp"
    systemctl disable --now kv4p-usrp.service 2>/dev/null || true
    rm -f "$SERVICE_FILE"
    rm -rf -- "$INSTALL_DIR"
    systemctl daemon-reload
    printf 'Removed the service and program. Configuration was preserved at %s\n' "$CONFIG_FILE"
    exit 0
fi

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    [[ ${ID:-} == "debian" || ${ID_LIKE:-} == *debian* ]] || \
        die "this installer currently supports Debian-based systems (including ASL3)"
else
    die "cannot identify this operating system"
fi

log "Installing operating-system dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates git libopus0 python3 python3-numpy python3-scipy python3-serial python3-venv

if id "$SERVICE_USER" >/dev/null 2>&1; then
    usermod -a -G dialout "$SERVICE_USER"
else
    useradd --system --user-group --home-dir "$INSTALL_DIR" --no-create-home \
        --shell /usr/sbin/nologin --groups dialout "$SERVICE_USER"
fi

if [[ -d "$INSTALL_DIR/.git" ]]; then
    log "Updating kv4p-usrp"
    git -C "$INSTALL_DIR" fetch --quiet origin main
    git -C "$INSTALL_DIR" checkout --quiet main
    git -C "$INSTALL_DIR" merge --ff-only --quiet origin/main
elif [[ -e "$INSTALL_DIR" ]]; then
    die "$INSTALL_DIR exists but is not a kv4p-usrp Git checkout"
else
    log "Downloading kv4p-usrp"
    git clone --depth 1 --branch main "$REPO_URL" "$INSTALL_DIR"
fi

python3 -m venv --system-site-packages "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/python" -c 'import numpy, scipy, serial'
chown -R root:root "$INSTALL_DIR"

prompt() {
    local message=$1 default=${2:-} answer
    [[ -r /dev/tty ]] || die "configuration is required; pass installer options when no terminal is available"
    if [[ -n $default ]]; then
        read -r -p "$message [$default]: " answer </dev/tty
        printf '%s' "${answer:-$default}"
    else
        read -r -p "$message: " answer </dev/tty
        printf '%s' "$answer"
    fi
}

validate_number() {
    [[ $2 =~ ^[0-9]+([.][0-9]+)?$ ]] || die "$1 must be a non-negative number"
}

write_config=1
if [[ -f "$CONFIG_FILE" && $RECONFIGURE -eq 0 && $CONFIG_REQUESTED -eq 0 ]]; then
    write_config=0
    log "Keeping existing configuration in $CONFIG_FILE"
fi

if [[ $write_config -eq 1 ]]; then
    if [[ -z $DEVICE ]]; then
        shopt -s nullglob
        serial_devices=(/dev/serial/by-id/* /dev/ttyUSB* /dev/ttyACM*)
        shopt -u nullglob
        default_device=${serial_devices[0]:-/dev/ttyUSB0}
        DEVICE=$(prompt "KV4P serial device" "$default_device")
    fi
    [[ $DEVICE =~ ^/dev/[A-Za-z0-9._:/-]+$ ]] || \
        die "device must be a path below /dev without spaces or shell characters"

    [[ -n $RX_FREQUENCY ]] || RX_FREQUENCY=$(prompt "RX frequency in MHz")
    [[ -n $TX_FREQUENCY ]] || TX_FREQUENCY=$(prompt "TX frequency in MHz")
    validate_number "RX frequency" "$RX_FREQUENCY"
    validate_number "TX frequency" "$TX_FREQUENCY"
    validate_number "RX gain" "$RX_GAIN"
    "$INSTALL_DIR/.venv/bin/python" -c \
        'import math, sys; g = float(sys.argv[1]); sys.exit(not (math.isfinite(g) and g > 0))' \
        "$RX_GAIN" || die "RX gain must be a finite number greater than zero"
    [[ $BANDWIDTH == keep || $BANDWIDTH == wide || $BANDWIDTH == narrow ]] || \
        die "bandwidth must be keep, wide, or narrow"
    validate_number "PTT hang" "$PTT_HANG"
    [[ $SQUELCH =~ ^[0-8]$ ]] || die "squelch must be from 0 to 8"
    [[ $POWER == high || $POWER == low ]] || die "power must be high or low"
    [[ $ALLSTAR_HOST =~ ^[A-Za-z0-9:._-]+$ ]] || die "invalid AllStar host"

    if [[ -z $RECEIVE_ONLY ]]; then
        answer=$(prompt "Start in receive-only mode? (recommended)" "Y")
        [[ $answer =~ ^[Nn]$ ]] && RECEIVE_ONLY=0 || RECEIVE_ONLY=1
    fi

    args="--device $DEVICE --rx-frequency $RX_FREQUENCY --tx-frequency $TX_FREQUENCY --squelch $SQUELCH --rx-gain $RX_GAIN --bandwidth $BANDWIDTH --power $POWER --ptt-hang $PTT_HANG --allstar-host $ALLSTAR_HOST"
    [[ $RECEIVE_ONLY -eq 1 ]] && args+=" --receive-only"
    umask 027
    printf '# Managed by the kv4p-usrp installer. Run install.sh --reconfigure to replace.\nKV4P_ARGS="%s"\n' \
        "$args" >"$CONFIG_FILE"
    chown root:"$SERVICE_USER" "$CONFIG_FILE"
fi

install -o root -g root -m 0644 "$INSTALL_DIR/packaging/kv4p-usrp.service" "$SERVICE_FILE"
systemctl daemon-reload

if [[ $NO_START -eq 0 ]]; then
    log "Enabling and starting kv4p-usrp"
    systemctl enable kv4p-usrp.service
    systemctl restart kv4p-usrp.service
else
    log "Installed without starting the service"
fi

cat <<EOF

kv4p-usrp is installed.

AllStar should contain this line in the appropriate node section of
/etc/asterisk/rpt.conf:

    rxchannel = USRP/127.0.0.1:34001:32001

Useful commands:
    systemctl status kv4p-usrp
    journalctl -u kv4p-usrp -f
    systemctl restart kv4p-usrp

Configuration: $CONFIG_FILE
EOF
