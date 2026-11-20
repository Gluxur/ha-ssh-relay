#!/usr/bin/env bash
# Checks the sshd and nginx configs written by relay-setup.sh against the real
# OpenSSH and nginx of the relay VM (Ubuntu 22.04: OpenSSH 8.9). Root, in a
# throwaway container (CI job "sshd", or locally):
#   podman run --rm -v "$PWD:/src:ro" -w /src ubuntu:22.04 bash -c \
#     'apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
#      openssh-server jq nginx libnginx-mod-stream && bash tests/integration/sshd_check.sh'
#
# Each case copies an sshd_config (legacy fixture or the stock one) to a temp
# file, writes the managed block twice (idempotency), then runs sshd -t and
# the script's own sshd -T checks (verify_sshd, verify_legacy_sshd) on it.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
FIXTURES=$ROOT/tests/fixtures
TENANTS=$ROOT/relay/tenants.example.json

# shellcheck source=relay/relay-setup.sh
source "$ROOT/relay/relay-setup.sh"

[[ $EUID -eq 0 ]] || die "must run as root (in a container)"

# Accounts sshd -T resolves: the admin, the legacy user, the tenants.
getent group "$RELAY_GROUP" >/dev/null || groupadd --system "$RELAY_GROUP"
id -u "$RELAY_ADMIN_USER" >/dev/null 2>&1 || useradd -m "$RELAY_ADMIN_USER"
id -u "$LEGACY_USER" >/dev/null 2>&1 || useradd -m "$LEGACY_USER"
for name in $(tenant_names "$TENANTS"); do
  id -u "relay-$name" >/dev/null 2>&1 || useradd -m -G "$RELAY_GROUP" -s /usr/sbin/nologin "relay-$name"
done
install -d -m 755 /run/sshd
ssh-keygen -A >/dev/null
# The global drop-in (PasswordAuthentication no), included by every config.
install -d -m 755 "$(dirname "$SSHD_GLOBAL")"
render_sshd_global >"$SSHD_GLOBAL"
STOCK=$(mktemp)
cp /etc/ssh/sshd_config "$STOCK"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK" "$STOCK"' EXIT
FAILED=0

# run_case <label> <sshd_config source> <none|keep|lock>
run_case() {
  local label=$1 source=$2 mode=$3
  SSHD_CONFIG=$WORK/$label
  LEGACY_MODE=$mode
  cp "$source" "$SSHD_CONFIG"
  if write_sshd_match "$SSHD_CONFIG" "$TENANTS" "$mode" \
    && cp "$SSHD_CONFIG" "$SSHD_CONFIG.1" \
    && write_sshd_match "$SSHD_CONFIG" "$TENANTS" "$mode" \
    && cmp -s "$SSHD_CONFIG" "$SSHD_CONFIG.1" \
    && sshd -t -f "$SSHD_CONFIG" \
    && verify_sshd "$TENANTS" \
    && verify_legacy_sshd; then
    printf 'ok      %s\n' "$label"
  else
    printf 'FAILED  %s\n' "$label"
    FAILED=1
  fi
}

# refuse_case <label> <sshd_config source> <none|keep|lock>: must fail, file untouched.
refuse_case() {
  local label=$1 source=$2 mode=$3
  SSHD_CONFIG=$WORK/$label
  cp "$source" "$SSHD_CONFIG"
  if ! write_sshd_match "$SSHD_CONFIG" "$TENANTS" "$mode" 2>/dev/null \
    && cmp -s "$source" "$SSHD_CONFIG"; then
    printf 'ok      %s (refused)\n' "$label"
  else
    printf 'FAILED  %s (not refused)\n' "$label"
    FAILED=1
  fi
}

ssh -V 2>&1 || true
run_case stock-none "$STOCK" none
for fixture in sshd_config_legacy_marked sshd_config_legacy_unmarked; do
  run_case "${fixture#sshd_config_}-keep" "$FIXTURES/$fixture" keep
  run_case "${fixture#sshd_config_}-lock" "$FIXTURES/$fixture" lock
done
run_case legacy_unparsable-lock "$FIXTURES/sshd_config_legacy_unparsable" lock
refuse_case legacy_unparsable-keep "$FIXTURES/sshd_config_legacy_unparsable" keep
for mode in none keep lock; do
  refuse_case "bare_gatewayports-$mode" "$FIXTURES/sshd_config_bare_gatewayports" "$mode"
done

# nginx: the rendered stream config, with and without 8123.
for NO_8123 in 0 1; do
  STAGE=$(mktemp -d)
  render_to_dir "$STAGE" "$TENANTS"
  configure_nginx "$TENANTS" "$STAGE"
  rm -rf "$STAGE"
  if nginx -t 2>"$WORK/nginx.log"; then
    printf 'ok      nginx-no_8123=%s\n' "$NO_8123"
  else
    cat "$WORK/nginx.log"
    printf 'FAILED  nginx-no_8123=%s\n' "$NO_8123"
    FAILED=1
  fi
done

((FAILED == 0)) || die "real sshd/nginx check failed"
log "real sshd/nginx check passed"
