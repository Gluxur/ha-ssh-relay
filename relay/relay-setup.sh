#!/usr/bin/env bash
# Idempotent multi-tenant configuration of the reverse-SSH relay.
#
# Each Home Assistant connects as relay-<tenant> and opens
# `ssh -R 127.0.0.1:<port>`. nginx (stream + ssl_preread) listens on 443 and
# 8123 and routes each TLS connection by SNI to the tenant's local port,
# without decrypting it.
#
# Usage:
#   sudo bash relay-setup.sh [--remove-legacy] <tenants.json>   apply (root)
#   bash relay-setup.sh --render <dir> <tenants.json>           render only
#
# tenants.json: {"tenants": {"<name>": {"domain": "...", "port": 18123,
#                                        "public_key": "ssh-ed25519 ..."}}}
#
# The functions can be sourced: main only runs when the script is executed.

set -euo pipefail

SSHD_CONFIG=/etc/ssh/sshd_config
SSHD_GLOBAL=/etc/ssh/sshd_config.d/00-ha-relay.conf
NGINX_CONF=/etc/nginx/nginx.conf
NGINX_STREAM_DIR=/etc/nginx/stream.d
NGINX_RELAY_CONF=/etc/nginx/stream.d/ha-relay.conf
RELAY_GROUP=ha-relay
LEGACY_USER=ha-tunnel
BEGIN_MARK='# BEGIN ha-relay'
END_MARK='# END ha-relay'

# ── Helpers ───────────────────────────────────────────────────────────────────

log() { printf 'relay-setup: %s\n' "$*"; }
warn() { printf 'relay-setup: WARNING: %s\n' "$*" >&2; }
die() {
  printf 'relay-setup: error: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat >&2 <<'EOF'
usage: relay-setup.sh [--remove-legacy] <tenants.json>
       relay-setup.sh --render <dir> <tenants.json>
EOF
  exit "${1:-1}"
}

# Tenant names, sorted.
tenant_names() {
  jq -r '.tenants | keys[]' "$1"
}

# "<name><TAB><port>" per tenant, sorted by name.
tenant_ports() {
  jq -r '.tenants | to_entries | sort_by(.key)[] | "\(.key)\t\(.value.port)"' "$1"
}

# ── Validation ────────────────────────────────────────────────────────────────

# Rules: name [a-z0-9-]{1,24}; port integer 18000-18999, unique; domain a
# lowercase DNS name, unique; public_key a single-line ssh-ed25519 key.
# shellcheck disable=SC2016  # jq program, not shell expansions
VALIDATE_JQ='
def err($n; $r): "tenant \($n): \($r)";
def dup($e; $i; $field; $value):
  [$e[:$i][] | select((.value | type) == "object" and .value[$field] == $value) | .key];
if type != "object" or (.tenants | type) != "object" then
  "tenants file: expected an object with a \"tenants\" object"
else
  (.tenants | to_entries | sort_by(.key)) as $e
  | range(0; $e | length) as $i
  | $e[$i] as {key: $n, value: $v}
  | if ($v | type) != "object" then err($n; "entry must be an object")
    else
      (if $n | test("\\A[a-z0-9-]{1,24}\\z") then empty
       else err($n; "invalid name (expected [a-z0-9-]{1,24})") end),
      (if ($v.port | type) == "number" and $v.port == ($v.port | floor)
          and $v.port >= 18000 and $v.port <= 18999 then
         dup($e; $i; "port"; $v.port) as $d
         | if $d == [] then empty
           else err($n; "port \($v.port) already used by tenant \($d[0])") end
       else err($n; "port must be an integer in 18000-18999") end),
      (if ($v.domain | type) == "string" and ($v.domain | test(
            "\\A[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+\\z")) then
         dup($e; $i; "domain"; $v.domain) as $d
         | if $d == [] then empty
           else err($n; "domain \($v.domain) already used by tenant \($d[0])") end
       else err($n; "invalid domain (expected a lowercase DNS name)") end),
      (if ($v.public_key | type) == "string" and ($v.public_key | test(
            "\\Assh-ed25519 [A-Za-z0-9+/]+={0,2}( [!-~][ -~]*)?\\z")) then empty
       else err($n; "public_key must be a single-line ssh-ed25519 key") end)
    end
end
'

# validate_tenants <file>: 0, or 1 with "tenant <name>: <reason>" on stderr.
validate_tenants() {
  local file=$1 errors
  if ! errors=$(jq -r "$VALIDATE_JQ" "$file" 2>&1); then
    printf 'tenants file: %s is not valid JSON: %s\n' "$file" "$errors" >&2
    return 1
  fi
  if [[ -n "$errors" ]]; then
    printf '%s\n' "$errors" >&2
    return 1
  fi
}

# ── Rendering (stdout, no side effects) ───────────────────────────────────────

# Drop-in included at the top of sshd_config: first value wins in sshd, so this
# beats a later "PasswordAuthentication yes" (e.g. 50-cloud-init.conf).
render_sshd_global() {
  cat <<'EOF'
# Managed by relay-setup.sh, do not edit.
PasswordAuthentication no
EOF
}

# Block appended at the end of sshd_config, between the markers. Not in
# sshd_config.d: the Include sits at the top of sshd_config, and an included
# Match would capture the rest of the main config.
render_sshd_match() {
  local file=$1 rows name port
  rows=$(tenant_ports "$file") || return 1
  printf '%s\n' "$BEGIN_MARK"
  cat <<'EOF'
# Managed by relay-setup.sh, do not edit between the markers.
Match Group ha-relay
    PasswordAuthentication no
    AuthenticationMethods publickey
    GatewayPorts no
    AllowTcpForwarding remote
    PermitOpen none
    AllowStreamLocalForwarding no
    X11Forwarding no
    AllowAgentForwarding no
    PermitTTY no
    ForceCommand /usr/sbin/nologin
EOF
  while IFS=$'\t' read -r name port; do
    [[ -n "$name" ]] || continue
    printf '\nMatch User relay-%s\n    PermitListen 127.0.0.1:%s\n' "$name" "$port"
  done <<<"$rows"
  printf '%s\n' "$END_MARK"
}

# render_nginx <file> [--no-8123]: stream config routing by SNI.
render_nginx() {
  local file=$1 no_8123=${2:-} rows domain port
  rows=$(jq -r '.tenants | [.[]] | sort_by(.domain)[] | "\(.domain)\t\(.port)"' "$file") || return 1
  printf '# Managed by relay-setup.sh, do not edit.\n'
  # shellcheck disable=SC2016  # nginx variables, not shell expansions
  printf 'map $ssl_preread_server_name $ha_relay_upstream {\n'
  while IFS=$'\t' read -r domain port; do
    [[ -n "$domain" ]] || continue
    printf '    %s 127.0.0.1:%s;\n' "$domain" "$port"
  done <<<"$rows"
  # Nothing listens on port 1: unknown names get their connection closed.
  printf '    default 127.0.0.1:1;\n}\n\n'
  printf 'server {\n    listen 443;\n'
  if [[ "$no_8123" != "--no-8123" ]]; then
    printf '    listen 8123;\n'
  fi
  # shellcheck disable=SC2016
  printf '    ssl_preread on;\n    proxy_pass $ha_relay_upstream;\n'
  printf '    proxy_connect_timeout 5s;\n}\n'
}

# render_authorized_keys <file> <name>
render_authorized_keys() {
  jq -r --arg n "$2" '
    (.tenants[$n] // error("unknown tenant \($n)"))
    | "restrict,port-forwarding,permitlisten=\"127.0.0.1:\(.port)\" \(.public_key)"
  ' "$1"
}

# render_to_dir <dir> <file> [--no-8123]: every rendered file, nothing applied.
render_to_dir() {
  local dir=$1 file=$2 no_8123=${3:-} name
  mkdir -p "$dir"
  render_sshd_global >"$dir/sshd_global.conf"
  render_sshd_match "$file" >"$dir/sshd_match.conf"
  render_nginx "$file" "$no_8123" >"$dir/nginx.conf"
  rm -rf "$dir/authorized_keys"
  mkdir "$dir/authorized_keys"
  for name in $(tenant_names "$file"); do
    render_authorized_keys "$file" "$name" >"$dir/authorized_keys/$name"
  done
}

# ── sshd_config editing ───────────────────────────────────────────────────────

# strip_legacy_sshd <file>: removes, in place, the marked block and everything
# from the first "GatewayPorts yes" line to the end of the file (the
# password-era block, same rule as migrate-to-key-auth.sh). Trailing blank
# lines are dropped so that strip + append is stable.
strip_legacy_sshd() {
  local file=$1 tmp
  if grep -qx "$BEGIN_MARK" "$file" && ! grep -qx "$END_MARK" "$file"; then
    printf '%s: "%s" without "%s", fix it by hand\n' "$file" "$BEGIN_MARK" "$END_MARK" >&2
    return 1
  fi
  tmp=$(mktemp) || return 1
  if ! sed -e "/^$BEGIN_MARK\$/,/^$END_MARK\$/d" -e '/^GatewayPorts yes$/,$d' "$file" \
    | awk '/^[[:space:]]*$/ { blank = blank $0 "\n"; next }
           { printf "%s%s\n", blank, $0; blank = "" }' >"$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  # cat keeps the inode, owner and mode of the original file.
  cat "$tmp" >"$file"
  rm -f "$tmp"
}

# write_sshd_match <sshd_config> <tenants.json>: strip, then append the block.
write_sshd_match() {
  local block
  block=$(render_sshd_match "$2") || return 1
  strip_legacy_sshd "$1" || return 1
  printf '\n%s\n' "$block" >>"$1"
}

# ── Apply steps (root) ────────────────────────────────────────────────────────

install_packages() {
  local pkg missing=()
  for pkg in nginx libnginx-mod-stream iptables-persistent jq; do
    # shellcheck disable=SC2016  # dpkg-query format, not a shell expansion
    if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then
      missing+=("$pkg")
    fi
  done
  if ((${#missing[@]})); then
    log "installing ${missing[*]}"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    apt-get install -y -q "${missing[@]}"
  fi
}

# PIDs listening on a TCP port.
port_pids() {
  ss -H -ltnp "sport = :$1" | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u || true
}

# "name(user) ..." of the processes listening on a TCP port, or "nothing".
port_holders() {
  local pid uid out=""
  for pid in $(port_pids "$1"); do
    uid=$(ps -o uid= -p "$pid" | tr -d ' ') || continue
    out+="$(ps -o comm= -p "$pid")($(getent passwd "$uid" | cut -d: -f1)) "
  done
  printf '%s\n' "${out:-nothing}"
}

# port_held_by_uid <port> <uid> (uid, since ps truncates long user names).
port_held_by_uid() {
  local pid
  for pid in $(port_pids "$1"); do
    if [[ "$(ps -o uid= -p "$pid" | tr -d ' ')" == "$2" ]]; then
      return 0
    fi
  done
  return 1
}

# Sets NO_8123=1 when the legacy ha-tunnel session keeps 8123 (no --remove-legacy).
handle_legacy() {
  local remove_legacy=$1 legacy_uid i
  NO_8123=0
  legacy_uid=$(id -u "$LEGACY_USER" 2>/dev/null) || return 0
  if ((remove_legacy)); then
    # Only the session is cut: the user and its authorized_keys stay (rollback).
    log "stopping $LEGACY_USER sessions (--remove-legacy)"
    pkill -u "$LEGACY_USER" || true
    for ((i = 0; i < 10; i++)); do
      port_held_by_uid 8123 "$legacy_uid" || return 0
      sleep 1
    done
    die "port 8123 still held by $LEGACY_USER after 10 s"
  elif port_held_by_uid 8123 "$legacy_uid"; then
    warn "8123 held by $LEGACY_USER (legacy); run with --remove-legacy to take it over"
    NO_8123=1
  fi
}

# Fails before any write if 443/8123 are taken by something other than nginx.
check_ports_free() {
  local port ports=(443)
  ((NO_8123)) || ports+=(8123)
  for port in "${ports[@]}"; do
    if [[ -n "$(port_pids "$port")" ]] \
      && ! ss -H -ltnp "sport = :$port" | grep -q '"nginx"'; then
      die "port $port held by $(port_holders "$port")"
    fi
  done
}

BACKUPS=()
CREATED=()

backup_configs() {
  local ts f
  ts=$(date +%Y%m%d%H%M%S)
  for f in "$SSHD_CONFIG" "$SSHD_GLOBAL" "$NGINX_CONF" "$NGINX_RELAY_CONF"; do
    if [[ -e "$f" ]]; then
      cp -p "$f" "$f.bak.$ts"
      BACKUPS+=("$f.bak.$ts")
    else
      CREATED+=("$f")
    fi
  done
  log "backups: ${BACKUPS[*]}"
}

restore_configs() {
  local b f
  for b in "${BACKUPS[@]}"; do
    cp -p "$b" "${b%.bak.*}"
  done
  for f in "${CREATED[@]}"; do
    rm -f "$f"
  done
}

ensure_user() {
  local name=$1 stage=$2 user="relay-$1" home
  if id -u "$user" >/dev/null 2>&1; then
    usermod -a -G "$RELAY_GROUP" -s /usr/sbin/nologin "$user"
  else
    log "creating $user"
    useradd -m -U -G "$RELAY_GROUP" -s /usr/sbin/nologin "$user"
  fi
  passwd -l "$user" >/dev/null
  home=$(getent passwd "$user" | cut -d: -f6)
  install -d -m 700 -o "$user" -g "$user" "$home/.ssh"
  install -m 600 -o "$user" -g "$user" "$stage/authorized_keys/$name" "$home/.ssh/authorized_keys"
}

remove_user() {
  local user=$1 i
  log "removing $user"
  # reload ssh does not cut running sessions: kill them first.
  pkill -u "$user" || true
  for ((i = 0; i < 5; i++)); do
    pgrep -u "$user" >/dev/null || break
    sleep 1
  done
  pkill -KILL -u "$user" || true
  userdel -r "$user"
}

sync_users() {
  local file=$1 stage=$2 name user members
  local -A wanted=()
  getent group "$RELAY_GROUP" >/dev/null || groupadd --system "$RELAY_GROUP"
  for name in $(tenant_names "$file"); do
    wanted[$name]=1
    ensure_user "$name" "$stage"
  done
  # ha-tunnel is not in the group, so it is never removed here.
  IFS=, read -ra members <<<"$(getent group "$RELAY_GROUP" | cut -d: -f4)"
  for user in "${members[@]}"; do
    if [[ "$user" != relay-* ]]; then
      warn "$user is in group $RELAY_GROUP but is not a relay-* user; left alone"
    elif [[ -z "${wanted[${user#relay-}]:-}" ]]; then
      remove_user "$user"
    fi
  done
}

configure_sshd() {
  local file=$1 stage=$2
  install -d -m 755 "$(dirname "$SSHD_GLOBAL")"
  install -m 644 "$stage/sshd_global.conf" "$SSHD_GLOBAL"
  if ! grep -qx 'Include /etc/ssh/sshd_config.d/\*.conf' "$SSHD_CONFIG"; then
    warn "$SSHD_CONFIG does not include sshd_config.d/*.conf; $SSHD_GLOBAL is ignored"
  fi
  write_sshd_match "$SSHD_CONFIG" "$file"
}

NGINX_RESTART=0

configure_nginx() {
  local file=$1 stage=$2
  if ((NO_8123)); then
    render_nginx "$file" --no-8123 >"$stage/nginx.conf"
  fi
  install -d -m 755 "$NGINX_STREAM_DIR"
  install -m 644 "$stage/nginx.conf" "$NGINX_RELAY_CONF"
  if ! grep -qx "$BEGIN_MARK" "$NGINX_CONF"; then
    printf '\n%s\nstream { include %s/*.conf; }\n%s\n' \
      "$BEGIN_MARK" "$NGINX_STREAM_DIR" "$END_MARK" >>"$NGINX_CONF"
    NGINX_RESTART=1
  fi
  # Nothing must listen on 80.
  rm -f /etc/nginx/sites-enabled/default
}

configure_firewall() {
  local port
  for port in 22 443 8123; do
    iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null \
      || iptables -I INPUT -p tcp --dport "$port" -j ACCEPT
  done
  netfilter-persistent save
}

# Admin user intact, each tenant limited to its own port.
verify_sshd() {
  local file=$1 out name port
  out=$(sshd -T -C user=ubuntu,host=x,addr=1.2.3.4) || return 1
  if ! grep -qx 'passwordauthentication no' <<<"$out"; then
    printf 'sshd -T (ubuntu): passwordauthentication is not "no"\n' >&2
    return 1
  fi
  if grep -q '^forcecommand' <<<"$out"; then
    printf 'sshd -T (ubuntu): a ForceCommand applies to the admin user\n' >&2
    return 1
  fi
  while IFS=$'\t' read -r name port; do
    [[ -n "$name" ]] || continue
    out=$(sshd -T -C "user=relay-$name,host=x,addr=1.2.3.4") || return 1
    if ! grep -qx "permitlisten 127.0.0.1:$port" <<<"$out"; then
      printf 'sshd -T (relay-%s): permitlisten 127.0.0.1:%s missing\n' "$name" "$port" >&2
      return 1
    fi
  done <<<"$(tenant_ports "$file")"
}

check_configs() {
  local file=$1
  install -d -m 755 /run/sshd
  if ! sshd -t; then
    restore_configs
    die "sshd -t failed; previous configuration restored"
  fi
  if ! nginx -t; then
    restore_configs
    die "nginx -t failed; previous configuration restored"
  fi
  if ! verify_sshd "$file"; then
    restore_configs
    die "sshd verification failed; previous configuration restored"
  fi
}

reload_services() {
  # reload: running sessions (yours included) stay up.
  systemctl reload ssh
  if ((NGINX_RESTART)) || ! systemctl is-active --quiet nginx; then
    systemctl restart nginx
  else
    systemctl reload nginx
  fi
}

# nginx -t does not see a taken port and reload does not report a failed bind.
check_listeners() {
  local port i ports=(443)
  ((NO_8123)) || ports+=(8123)
  for port in "${ports[@]}"; do
    for ((i = 0; ; i++)); do
      if ss -H -ltnp "sport = :$port" | grep -q '"nginx"'; then
        break
      fi
      ((i < 5)) || die "nginx is not listening on $port (held by $(port_holders "$port"))"
      sleep 1
    done
  done
  if ((NO_8123)); then
    warn "8123 held by $LEGACY_USER (legacy); run with --remove-legacy to take it over"
  fi
}

show_fingerprint() {
  log "host key fingerprint (pinned by the integration):"
  ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
}

# ── Main ──────────────────────────────────────────────────────────────────────

main() {
  local remove_legacy=0 render_dir="" file="" stage
  while (($#)); do
    case "$1" in
      --remove-legacy) remove_legacy=1 ;;
      --render)
        (($# >= 2)) || usage
        render_dir=$2
        shift
        ;;
      -h | --help) usage 0 ;;
      -*) usage ;;
      *)
        [[ -z "$file" ]] || usage
        file=$1
        ;;
    esac
    shift
  done
  [[ -n "$file" ]] || usage
  [[ -r "$file" ]] || die "cannot read $file"

  if [[ -n "$render_dir" ]]; then
    command -v jq >/dev/null || die "jq is required"
    validate_tenants "$file" || exit 1
    render_to_dir "$render_dir" "$file"
    return 0
  fi

  [[ $EUID -eq 0 ]] || die "must run as root (sudo bash relay-setup.sh ...)"

  install_packages                            # 1
  validate_tenants "$file" || exit 1          # 2: nothing modified on failure
  stage=$(mktemp -d)
  # shellcheck disable=SC2064  # expand now: stage is local
  trap "rm -rf '$stage'" EXIT
  render_to_dir "$stage" "$file"
  handle_legacy "$remove_legacy"
  check_ports_free
  backup_configs
  sync_users "$file" "$stage"                 # 3
  configure_sshd "$file" "$stage"             # 4
  configure_nginx "$file" "$stage"            # 5
  configure_firewall                          # 6
  check_configs "$file"                       # 7
  reload_services
  check_listeners                             # 8
  show_fingerprint                            # 9
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
