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

# require_tenants <file>: apply refuses an empty list (validate and --render
# accept it), since it would remove every relay user.
require_tenants() {
  local count
  count=$(jq '.tenants | length' "$1") || return 1
  if [[ "$count" == 0 ]]; then
    printf 'tenants file: no tenants in %s; refusing to apply (every relay user would be removed)
' "$1" >&2
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
# render_sshd_match <file> [<legacy body>]: with a body, a last section
# "Match User ha-tunnel" + that body (verbatim) is added for the legacy user.
render_sshd_match() {
  local file=$1 legacy_body=${2:-} rows name port
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
  if [[ -n "$legacy_body" ]]; then
    printf '\n# Legacy user: kept as found until relay-setup.sh --remove-legacy (then locked).\n'
    printf 'Match User %s\n%s\n' "$LEGACY_USER" "$legacy_body"
  fi
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

# Legacy section written with --remove-legacy: ha-tunnel keeps its user and
# authorized_keys (rollback stays possible) but can no longer forward anything.
LEGACY_LOCKED_BODY='    AllowTcpForwarding no
    PasswordAuthentication no
    PermitTTY no
    ForceCommand /usr/sbin/nologin'

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

# extract_legacy_sshd <file>: prints, verbatim, the body of the
# "Match User ha-tunnel" section that strip_legacy_sshd would remove: from the
# marked block first (legacy cloud-init/migrate block, or the section this
# script keeps there), else from the unmarked "GatewayPorts yes" tail, where
# the global "GatewayPorts yes" is carried into the body if it lacks one.
# Prints nothing when there is no such section.
extract_legacy_sshd() {
  awk -v begin="$BEGIN_MARK" -v end="$END_MARK" -v user="$LEGACY_USER" '
    function is_match(line) { return tolower(line) ~ /^[[:space:]]*match[[:space:]]/ }
    function is_legacy(line,  n, f) {
      n = split(line, f, " ")
      return n == 3 && tolower(f[1]) == "match" && tolower(f[2]) == "user" && f[3] == user
    }
    $0 == begin { zone = "marked"; cap = ""; next }
    $0 == end { zone = (tail ? "unmarked" : ""); cap = ""; next }
    zone == "" && $0 == "GatewayPorts yes" { tail = 1; zone = "unmarked"; next }
    zone != "" && is_match($0) { cap = is_legacy($0) ? zone : ""; next }
    cap != "" { body[cap] = body[cap] $0 "\n" }
    END {
      if ("marked" in body) out = body["marked"]
      else if ("unmarked" in body) {
        out = body["unmarked"]
        if (tolower(out) !~ /(^|\n)[[:space:]]*gatewayports[[:space:]]/)
          out = out "    GatewayPorts yes\n"
      }
      sub(/[[:space:]]+$/, "", out)
      if (out != "") print out
    }
  ' "$1"
}

# write_sshd_match <sshd_config> <tenants.json> [none|keep|lock]: strip, then
# append the managed block. keep: the legacy ha-tunnel section is carried over
# verbatim; lock: it is replaced by LEGACY_LOCKED_BODY; none (default): dropped.
write_sshd_match() {
  local config=$1 file=$2 mode=${3:-none} body="" block
  case "$mode" in
    none) ;;
    keep) body=$(extract_legacy_sshd "$config") || return 1 ;;
    lock) body=$LEGACY_LOCKED_BODY ;;
    *)
      printf 'write_sshd_match: unknown legacy mode %s\n' "$mode" >&2
      return 1
      ;;
  esac
  block=$(render_sshd_match "$file" "$body") || return 1
  strip_legacy_sshd "$config" || return 1
  printf '\n%s\n' "$block" >>"$config"
}

# ── Apply steps (root) ────────────────────────────────────────────────────────

install_packages() {
  local pkg status missing=()
  for pkg in nginx libnginx-mod-stream iptables-persistent jq; do
    # shellcheck disable=SC2016  # dpkg-query format, not a shell expansion
    status=$(dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null) || status=""
    [[ "$status" == "install ok installed" ]] || missing+=("$pkg")
  done
  if ((${#missing[@]})); then
    log "installing ${missing[*]}"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    apt-get install -y -q "${missing[@]}"
  fi
}

# PIDs listening on a TCP port (ss output captured first: no SIGPIPE).
port_pids() {
  local out
  out=$(ss -H -ltnp "sport = :$1") || return 0
  grep -o 'pid=[0-9]*' <<<"$out" | cut -d= -f2 | sort -u || true
}

pid_uid() { ps -o uid= -p "$1" | tr -d ' '; }
pid_comm() { ps -o comm= -p "$1"; }

# "name(user) " for one PID.
pid_desc() {
  local uid
  uid=$(pid_uid "$1") || return 0
  printf '%s(%s) ' "$(pid_comm "$1")" "$(getent passwd "$uid" | cut -d: -f1)"
}

# "name(user) ..." of the processes listening on a TCP port, or "nothing".
port_holders() {
  local pid out=""
  for pid in $(port_pids "$1"); do
    out+=$(pid_desc "$pid")
  done
  printf '%s\n' "${out:-nothing}"
}

# port_foreign_holders <port> [<ignored uid>]: non-nginx listeners, or nothing.
port_foreign_holders() {
  local pid out=""
  for pid in $(port_pids "$1"); do
    [[ "$(pid_comm "$pid")" != nginx ]] || continue
    [[ -z "${2:-}" || "$(pid_uid "$pid")" != "$2" ]] || continue
    out+=$(pid_desc "$pid")
  done
  printf '%s' "$out"
}

port_has_nginx() {
  local pid
  for pid in $(port_pids "$1"); do
    [[ "$(pid_comm "$pid")" != nginx ]] || return 0
  done
  return 1
}

# port_held_by_uid <port> <uid> (uid, since ps truncates long user names).
port_held_by_uid() {
  local pid
  for pid in $(port_pids "$1"); do
    [[ "$(pid_uid "$pid")" != "$2" ]] || return 0
  done
  return 1
}

# detect_legacy sets LEGACY_MODE (none|keep|lock), LEGACY_UID and NO_8123.
LEGACY_MODE=none
LEGACY_UID=""
NO_8123=0

detect_legacy() {
  local remove_legacy=$1
  if ! LEGACY_UID=$(id -u "$LEGACY_USER" 2>/dev/null); then
    LEGACY_UID=""
    if ((remove_legacy)); then
      log "no $LEGACY_USER user: --remove-legacy has nothing to do"
    fi
    return 0
  fi
  if ((remove_legacy)); then
    LEGACY_MODE=lock
  else
    LEGACY_MODE=keep
    if port_held_by_uid 8123 "$LEGACY_UID"; then
      warn "8123 held by $LEGACY_USER (legacy); run with --remove-legacy to take it over"
      NO_8123=1
    fi
  fi
}

# Fails before any write if 443/8123 are taken by something other than nginx
# (or than ha-tunnel on 8123, when --remove-legacy will take it over).
check_ports_free() {
  local holders ignore=""
  [[ "$LEGACY_MODE" != lock ]] || ignore=$LEGACY_UID
  holders=$(port_foreign_holders 443)
  [[ -z "$holders" ]] || die "port 443 held by $holders"
  if ! ((NO_8123)); then
    holders=$(port_foreign_holders 8123 "$ignore")
    [[ -z "$holders" ]] || die "port 8123 held by $holders"
  fi
}

# ── Backups and rollback ──────────────────────────────────────────────────────

BACKUPS=()
CREATED=()
ROLLBACK_ARMED=0
SSHD_RELOADED=0
NGINX_RELOADED=0
STAGE=""
NGINX_DEFAULT_SITE=/etc/nginx/sites-enabled/default
DEFAULT_SITE_LINK=""

# Backups sit next to each file and match no include glob (*.conf). The
# sites-enabled link is never copied there (nginx includes sites-enabled/*):
# its target is recorded instead.
backup_configs() {
  local ts f
  ts=$(date +%Y%m%d%H%M%S)
  if [[ -L "$NGINX_DEFAULT_SITE" ]]; then
    DEFAULT_SITE_LINK=$(readlink "$NGINX_DEFAULT_SITE")
  fi
  for f in "$SSHD_CONFIG" "$SSHD_GLOBAL" "$NGINX_CONF" "$NGINX_RELAY_CONF"; do
    if [[ -e "$f" ]]; then
      cp -a "$f" "$f.bak.$ts"
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
    f=${b%.bak.*}
    rm -f "$f"
    cp -a "$b" "$f" || warn "could not restore $f from $b"
  done
  for f in "${CREATED[@]}"; do
    rm -f "$f"
  done
  if [[ -n "$DEFAULT_SITE_LINK" ]]; then
    ln -sfn "$DEFAULT_SITE_LINK" "$NGINX_DEFAULT_SITE" || warn "could not restore $NGINX_DEFAULT_SITE"
  fi
}

# EXIT trap of the apply path. While armed (from the first write until the
# listeners are checked), any exit restores the backups and the services.
on_exit() {
  local rc=$?
  if ((ROLLBACK_ARMED)); then
    ROLLBACK_ARMED=0
    warn "apply failed; restoring the previous configuration"
    restore_configs
    if ((SSHD_RELOADED)); then
      systemctl reload ssh || warn "reload ssh failed after restore"
    fi
    if ((NGINX_RELOADED)); then
      systemctl restart nginx || warn "restart nginx failed after restore"
    fi
    ((rc)) || rc=1
  fi
  [[ -z "$STAGE" ]] || rm -rf "$STAGE"
  exit "$rc"
}

# ── Users, sshd, nginx, firewall ──────────────────────────────────────────────

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
    if [[ "$user" != relay-?* ]]; then
      warn "$user is in group $RELAY_GROUP but is not a relay-<name> user; left alone"
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
  write_sshd_match "$SSHD_CONFIG" "$file" "$LEGACY_MODE"
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
  rm -f "$NGINX_DEFAULT_SITE"
}

configure_firewall() {
  local port
  for port in 22 443 8123; do
    iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null \
      || iptables -I INPUT -p tcp --dport "$port" -j ACCEPT
  done
  netfilter-persistent save
}

# ── Checks and reloads ────────────────────────────────────────────────────────

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

# Failures exit through die: the EXIT trap restores the backups.
check_configs() {
  local file=$1
  install -d -m 755 /run/sshd
  sshd -t || die "sshd -t failed"
  nginx -t || die "nginx -t failed"
  verify_sshd "$file" || die "sshd verification failed"
}

reload_ssh() {
  # reload: running sessions (yours included) stay up.
  SSHD_RELOADED=1
  systemctl reload ssh
}

# --remove-legacy, after reload_ssh: ha-tunnel can no longer forward, so once
# its session is cut it cannot take 8123 back. User and keys are kept.
take_over_8123() {
  local i holders
  [[ "$LEGACY_MODE" == lock ]] || return 0
  log "stopping $LEGACY_USER sessions (--remove-legacy)"
  pkill -u "$LEGACY_USER" || true
  for ((i = 0; i < 10; i++)); do
    port_held_by_uid 8123 "$LEGACY_UID" || break
    sleep 1
  done
  holders=$(port_foreign_holders 8123)
  [[ -z "$holders" ]] || die "port 8123 still held by $holders after stopping $LEGACY_USER"
}

reload_nginx() {
  NGINX_RELOADED=1
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
      if port_has_nginx "$port"; then
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
  local remove_legacy=0 render_dir="" file=""
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
  require_tenants "$file" || exit 1
  trap on_exit EXIT
  STAGE=$(mktemp -d)
  render_to_dir "$STAGE" "$file"
  detect_legacy "$remove_legacy"
  check_ports_free
  backup_configs
  ROLLBACK_ARMED=1                            # from here, any exit rolls back
  sync_users "$file" "$STAGE"                 # 3
  configure_sshd "$file" "$STAGE"             # 4
  configure_nginx "$file" "$STAGE"            # 5
  configure_firewall                          # 6
  check_configs "$file"                       # 7
  reload_ssh
  take_over_8123
  reload_nginx
  check_listeners                             # 8
  ROLLBACK_ARMED=0
  show_fingerprint                            # 9
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
