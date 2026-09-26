#!/usr/bin/env bash
#
# Provision an LXC on a Proxmox VE host, install jwt-notify into it and run it
# as a systemd service.
#
# Run this ON the Proxmox host, as root:
#
#     ./proxmox-lxc.sh
#
# Everything is overridable by environment variable, e.g.:
#
#     VMID=142 MEMORY=1024 IPV4=192.168.1.50/24 GATEWAY=192.168.1.1 ./proxmox-lxc.sh
#
# For a private repository, export a token with read access first:
#
#     GITHUB_TOKEN=ghp_xxx ./proxmox-lxc.sh
#
set -euo pipefail

# --- configuration -----------------------------------------------------------

VMID="${VMID:-}"                     # default: next free id from the cluster
CT_HOSTNAME="${CT_HOSTNAME:-jwt-notify}"
PASSWORD="${PASSWORD:-}"             # default: a random one, printed at the end

STORAGE="${STORAGE:-local-lvm}"      # where the rootfs lives
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
TEMPLATE_NAME="${TEMPLATE_NAME:-debian-12-standard}"

DISK="${DISK:-4}"                    # GiB
CORES="${CORES:-1}"
MEMORY="${MEMORY:-512}"              # MiB
SWAP="${SWAP:-512}"                  # MiB

BRIDGE="${BRIDGE:-vmbr0}"
IPV4="${IPV4:-dhcp}"                 # or e.g. 192.168.1.50/24
GATEWAY="${GATEWAY:-}"               # required when IPV4 is static
VLAN="${VLAN:-}"                     # optional VLAN tag

REPO_URL="${REPO_URL:-https://github.com/devachnid/jwt-notify}"
BRANCH="${BRANCH:-master}"          # the branch or tag to deploy
APP_DIR="${APP_DIR:-/opt/jwt-notify}"
SERVICE_USER="${SERVICE_USER:-jwtnotify}"
PORT="${PORT:-8000}"

# Service configuration. Without NOTIFY_API_KEY the proxy routes return 503
# and only /token works. PROXY_KEY is generated when not given, unless
# PROXY_AUTH=none says an authenticating front end (such as Cloudflare Access)
# guards the service instead.
NOTIFY_API_KEY="${NOTIFY_API_KEY:-}"
PROXY_KEY="${PROXY_KEY:-}"
PROXY_AUTH="${PROXY_AUTH:-}"
ENV_FILE="${ENV_FILE:-/etc/jwt-notify.env}"

START_ON_BOOT="${START_ON_BOOT:-1}"

# --- helpers -----------------------------------------------------------------

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

in_ct() { pct exec "$VMID" -- "$@"; }

# Run a shell snippet inside the container. Passed on stdin so quoting in the
# snippet survives intact.
in_ct_sh() { pct exec "$VMID" -- bash -euo pipefail -c "$1"; }

# --- preflight ---------------------------------------------------------------

[ "$(id -u)" -eq 0 ] || die "must run as root on the Proxmox host"
command -v pct >/dev/null || die "pct not found — is this a Proxmox VE host?"

if [ -z "$VMID" ]; then
    VMID="$(pvesh get /cluster/nextid)"
    log "using next free VMID: $VMID"
fi

if pct status "$VMID" >/dev/null 2>&1; then
    die "container $VMID already exists — set VMID to a free id"
fi

if [ "$IPV4" != "dhcp" ] && [ -z "$GATEWAY" ]; then
    die "GATEWAY is required when IPV4 is a static address"
fi

if [ -z "$PASSWORD" ]; then
    PASSWORD="$(openssl rand -base64 18)"
    GENERATED_PASSWORD=1
fi

case "$PROXY_AUTH" in
    none)
        [ -z "$PROXY_KEY" ] || die "PROXY_AUTH=none and PROXY_KEY are both set — unset one"
        ;;
    ""|key)
        if [ -z "$PROXY_KEY" ]; then
            PROXY_KEY="$(openssl rand -hex 32)"
            GENERATED_PROXY_KEY=1
        fi
        ;;
    *)
        die "PROXY_AUTH must be 'key' or 'none', got '$PROXY_AUTH'"
        ;;
esac

# --- template ----------------------------------------------------------------

log "looking for a $TEMPLATE_NAME template"
TEMPLATE="$(pveam list "$TEMPLATE_STORAGE" 2>/dev/null \
    | awk -v n="$TEMPLATE_NAME" '$1 ~ n {print $1}' | sort | tail -1 || true)"

if [ -z "$TEMPLATE" ]; then
    log "not present locally — downloading"
    pveam update
    AVAILABLE="$(pveam available --section system \
        | awk -v n="$TEMPLATE_NAME" '$2 ~ n {print $2}' | sort | tail -1)"
    [ -n "$AVAILABLE" ] || die "no $TEMPLATE_NAME template available from pveam"
    pveam download "$TEMPLATE_STORAGE" "$AVAILABLE"
    TEMPLATE="${TEMPLATE_STORAGE}:vztmpl/${AVAILABLE}"
fi
log "template: $TEMPLATE"

# --- create ------------------------------------------------------------------

NET="name=eth0,bridge=${BRIDGE},ip=${IPV4}"
[ -n "$GATEWAY" ] && NET="${NET},gw=${GATEWAY}"
[ -n "$VLAN" ] && NET="${NET},tag=${VLAN}"

log "creating container $VMID ($CT_HOSTNAME)"
# nesting=1 lets systemd inside the container create the mount namespaces the
# unit's ProtectSystem/PrivateTmp sandboxing relies on. In an unprivileged
# container it grants nothing on the host.
pct create "$VMID" "$TEMPLATE" \
    --hostname "$CT_HOSTNAME" \
    --password "$PASSWORD" \
    --unprivileged 1 \
    --cores "$CORES" \
    --memory "$MEMORY" \
    --swap "$SWAP" \
    --rootfs "${STORAGE}:${DISK}" \
    --net0 "$NET" \
    --features nesting=1 \
    --onboot "$START_ON_BOOT" \
    --description "jwt-notify — JWT generator for GOV.UK Notify"

log "starting container"
pct start "$VMID"

log "waiting for the container to come up"
for _ in $(seq 1 60); do
    if in_ct test -d /run/systemd/system 2>/dev/null; then break; fi
    sleep 1
done
in_ct test -d /run/systemd/system || die "container did not finish booting"

log "waiting for network"
for _ in $(seq 1 60); do
    if in_ct getent hosts github.com >/dev/null 2>&1; then break; fi
    sleep 1
done
in_ct getent hosts github.com >/dev/null 2>&1 \
    || die "container has no working DNS/network — check bridge $BRIDGE and IPV4=$IPV4"

# --- install -----------------------------------------------------------------

log "installing packages"
in_ct_sh 'export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates curl'

CLONE_URL="$REPO_URL"
if [ -n "${GITHUB_TOKEN:-}" ]; then
    CLONE_URL="https://x-access-token:${GITHUB_TOKEN}@${REPO_URL#https://}"
fi

log "cloning $REPO_URL ($BRANCH)"
# The URL may embed a token, so it goes in on stdin: anything on the command
# line (including env VAR=... arguments) shows in ps on the host and in the
# container while the command runs.
# shellcheck disable=SC2016  # these expand inside the container, not on the host
printf '%s\n' "$CLONE_URL" | pct exec "$VMID" -- env BRANCH="$BRANCH" APP_DIR="$APP_DIR" \
    bash -euo pipefail -c '
IFS= read -r CLONE_URL
rm -rf "$APP_DIR"
# -c credential.helper= stops git caching the token anywhere.
git -c credential.helper= clone --quiet --depth 1 --branch "$BRANCH" "$CLONE_URL" "$APP_DIR"
# Drop any credential that git may have recorded in the remote URL.
git -C "$APP_DIR" remote set-url origin "$(git -C "$APP_DIR" remote get-url origin | sed -E "s#//[^@]*@#//#")"
'

log "creating virtualenv and installing dependencies"
in_ct_sh "python3 -m venv ${APP_DIR}/.venv
${APP_DIR}/.venv/bin/pip install --quiet --upgrade pip
${APP_DIR}/.venv/bin/pip install --quiet --require-hashes -r ${APP_DIR}/requirements.lock"

log "creating service user"
in_ct_sh "id -u ${SERVICE_USER} >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin ${SERVICE_USER}
chown -R ${SERVICE_USER}:${SERVICE_USER} ${APP_DIR}"

log "writing $ENV_FILE"
# The contents go in on stdin so no secret appears on a command line — on the
# host or in the container — and the file is readable by root only.
{
    [ -n "$NOTIFY_API_KEY" ] && printf 'NOTIFY_API_KEY=%s\n' "$NOTIFY_API_KEY"
    [ -n "$PROXY_KEY" ] && printf 'PROXY_KEY=%s\n' "$PROXY_KEY"
    [ "$PROXY_AUTH" = none ] && printf 'PROXY_AUTH=none\n'
    true
} | pct exec "$VMID" -- env ENV_FILE="$ENV_FILE" bash -euo pipefail -c '
umask 077
cat > "$ENV_FILE"
chown root:root "$ENV_FILE"
chmod 600 "$ENV_FILE"
'

log "installing systemd unit"
in_ct_sh "cat > /etc/systemd/system/jwt-notify.service <<UNIT
[Unit]
Description=jwt-notify — JWT generator for GOV.UK Notify
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
ExecStart=${APP_DIR}/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
Restart=on-failure
RestartSec=2

# The service handles live API keys: no writable paths, no privileges.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now jwt-notify.service"

# --- verify ------------------------------------------------------------------

log "waiting for the service to answer"
for _ in $(seq 1 30); do
    if in_ct curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then break; fi
    sleep 1
done

if ! in_ct curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    warn "service did not answer on /health — recent logs:"
    in_ct journalctl -u jwt-notify.service --no-pager -n 40 || true
    die "deployment failed"
fi

CT_IP="$(in_ct hostname -I 2>/dev/null | awk '{print $1}')"

log "smoke test with the documented example key"
# The X-Proxy-Key header is read from stdin (-H @-) to keep it off the command line.
if [ -n "$PROXY_KEY" ]; then printf 'X-Proxy-Key: %s\n' "$PROXY_KEY"; fi \
    | pct exec "$VMID" -- curl -s -X POST "http://127.0.0.1:${PORT}/token" \
        -H @- \
        -H 'Content-Type: application/json' \
        -d '{"api_key":"my_test_key-26785a09-ab16-4eb0-8407-a37497a57506-3d844edf-8d35-48ac-975b-e847b4f122b0"}'
echo

cat <<SUMMARY

  Container   $VMID ($CT_HOSTNAME)
  Address     ${CT_IP:-unknown}
  Service     http://${CT_IP:-<ip>}:${PORT}
  Health      curl http://${CT_IP:-<ip>}:${PORT}/health
  Logs        pct exec $VMID -- journalctl -u jwt-notify -f
  Config      $ENV_FILE (in the container), then systemctl restart jwt-notify
  Update      pct exec $VMID -- bash -c 'git -C ${APP_DIR} pull && ${APP_DIR}/.venv/bin/pip install -q --require-hashes -r ${APP_DIR}/requirements.lock && systemctl restart jwt-notify'
SUMMARY

if [ -z "$NOTIFY_API_KEY" ]; then
    warn "no NOTIFY_API_KEY was given, so the /v2 proxy routes will return 503."
    warn "add it to $ENV_FILE in the container and restart the service."
fi

if [ "$PROXY_AUTH" = none ]; then
    warn "PROXY_AUTH=none: anyone who can reach the service can send messages"
    warn "as your Notify service. Only run it like this behind an authenticating"
    warn "front end such as Cloudflare Access."
elif [ -n "${GENERATED_PROXY_KEY:-}" ]; then
    echo "  Proxy key   ${PROXY_KEY}"
    echo
    warn "PROXY_KEY was generated — callers send it as X-Proxy-Key. It is also in"
    warn "$ENV_FILE inside the container."
fi

if [ -n "${GENERATED_PASSWORD:-}" ]; then
    echo "  Root pw     ${PASSWORD}"
    echo
    warn "root password was generated — save it now, it is not stored anywhere"
fi

warn "the service takes live Notify API keys over plain HTTP — keep it on a trusted"
warn "network, or put a TLS-terminating reverse proxy in front of it."
