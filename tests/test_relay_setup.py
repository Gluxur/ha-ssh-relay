"""relay/relay-setup.sh: validation, rendering and legacy sshd cleanup (bash + jq)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = "relay/relay-setup.sh"
FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE = ROOT / "relay" / "tenants.example.json"

T = {
    "domain": "alice.example",
    "port": 18123,
    "public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample x",
}

# Lines of the stock Ubuntu 22.04 sshd_config that stripping must keep.
UBUNTU_LINES = [
    "Include /etc/ssh/sshd_config.d/*.conf",
    "KbdInteractiveAuthentication no",
    "UsePAM yes",
    "X11Forwarding yes",
    "PrintMotd no",
    "AcceptEnv LANG LC_*",
    "Subsystem\tsftp\t/usr/lib/openssh/sftp-server",
]


def _find_bash() -> str | None:
    if os.name != "nt":
        return shutil.which("bash")
    # On Windows, System32\bash.exe is WSL: use Git Bash instead.
    candidates = []
    git = shutil.which("git")
    if git:
        for parent in Path(git).resolve().parents:
            candidates.append(parent / "bin" / "bash.exe")
    candidates.append(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe")
    for candidate in candidates:
        if candidate.is_file() and "system32" not in str(candidate).lower():
            return str(candidate)
    return None


# Native Windows jq writes CRLF; --binary keeps LF like on Linux. A function
# (exported to child shells) wins over PATH, which Git Bash reorders.
PRELUDE = 'jq() { command jq -b "$@"; }; export -f jq; ' if os.name == "nt" else ""


BASH = _find_bash()
pytestmark = pytest.mark.skipif(
    BASH is None or shutil.which("jq") is None, reason="needs bash and jq"
)


def _bash(*args: str, func: str | None = None) -> subprocess.CompletedProcess:
    """Run a function of the sourced script, or the script itself when func is None."""
    if func is None:
        code = f'{PRELUDE}bash {SCRIPT} "$@"'
    else:
        code = f'{PRELUDE}source {SCRIPT}; {func} "$@"'
    return _run(code, *args)


def _run(code: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, "-c", code, "relay-setup", *args],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )


def _tenants_file(tmp_path: Path, tenants: dict) -> str:
    path = tmp_path / "tenants.json"
    path.write_bytes(json.dumps({"tenants": tenants}).encode())
    return path.as_posix()


@pytest.mark.parametrize(
    "tenants,reason",
    [
        ({"Bad_Name": T}, "name"),
        ({"a": {**T, "port": 17999}}, "port"),
        ({"a": T, "b": {**T, "domain": "other.example"}}, "port"),  # duplicate port
        ({"a": T, "b": {**T, "port": 18124}}, "domain"),  # duplicate domain
        ({"a": {**T, "public_key": "ssh-rsa AAAA"}}, "public_key"),
        ({"a": {k: v for k, v in T.items() if k != "public_key"}}, "public_key"),
    ],
)
def test_validate_rejects(tmp_path, tenants, reason):
    result = _bash(_tenants_file(tmp_path, tenants), func="validate_tenants")
    assert result.returncode == 1, result
    assert "tenant " in result.stderr
    assert reason in result.stderr


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"other": {}}),
        json.dumps({"tenants": {"a": {**T, "public_key": T["public_key"] + "\nssh-ed25519 AAAA y"}}}),
        json.dumps({"tenants": {"a": {**T, "domain": "alice.example; listen 80"}}}),
        json.dumps({"tenants": {"a": {**T, "port": "18123"}}}),
    ],
)
def test_validate_rejects_malformed(tmp_path, content):
    path = tmp_path / "tenants.json"
    path.write_bytes(content.encode())
    result = _bash(path.as_posix(), func="validate_tenants")
    assert result.returncode == 1, result
    assert result.stderr


def test_validate_accepts_example():
    result = _bash(EXAMPLE.as_posix(), func="validate_tenants")
    assert result.returncode == 0, result
    assert result.stderr == ""


def test_render_sshd_global():
    result = _bash(func="render_sshd_global")
    assert result.returncode == 0, result
    assert "PasswordAuthentication no" in result.stdout.splitlines()


def test_render_sshd_match_per_tenant(tmp_path):
    tenants = {"alice": T, "abc": {**T, "domain": "abc.example", "port": 18124}}
    result = _bash(_tenants_file(tmp_path, tenants), func="render_sshd_match")
    assert result.returncode == 0, result
    lines = [line.strip() for line in result.stdout.splitlines()]
    assert lines[0] == "# BEGIN ha-relay"
    assert lines[-1] == "# END ha-relay"
    group = lines.index("Match Group ha-relay")
    for directive in [
        "PasswordAuthentication no",
        "AuthenticationMethods publickey",
        "GatewayPorts no",
        "AllowTcpForwarding remote",
        "PermitOpen none",
        "AllowStreamLocalForwarding no",
        "X11Forwarding no",
        "AllowAgentForwarding no",
        "PermitTTY no",
        "ForceCommand /usr/sbin/nologin",
    ]:
        assert lines.index(directive) > group
    alice = lines.index("Match User relay-alice")
    assert lines[alice + 1] == "PermitListen 127.0.0.1:18123"
    abc = lines.index("Match User relay-abc")
    assert lines[abc + 1] == "PermitListen 127.0.0.1:18124"
    assert group < abc < alice  # sorted by name
    assert "GatewayPorts yes" not in result.stdout


def test_render_authorized_keys(tmp_path):
    result = _bash(
        _tenants_file(tmp_path, {"alice": T}), "alice", func="render_authorized_keys"
    )
    assert result.returncode == 0, result
    assert result.stdout == (
        'restrict,port-forwarding,permitlisten="127.0.0.1:18123" '
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample x\n"
    )


def test_render_nginx_default_closes(tmp_path):
    tenants = {"alice": T, "abc": {**T, "domain": "zzz.example", "port": 18124}}
    result = _bash(_tenants_file(tmp_path, tenants), func="render_nginx")
    assert result.returncode == 0, result
    out = result.stdout
    assert "map $ssl_preread_server_name $ha_relay_upstream {" in out
    assert "alice.example 127.0.0.1:18123;" in out
    assert out.index("alice.example") < out.index("zzz.example")  # sorted by domain
    assert "default 127.0.0.1:1;" in out
    assert "listen 443;" in out
    assert "listen 8123;" in out
    assert "ssl_preread on;" in out
    assert "proxy_pass $ha_relay_upstream;" in out
    assert "proxy_connect_timeout 5s;" in out
    assert "listen 80" not in out


def test_render_nginx_no_8123(tmp_path):
    result = _bash(
        _tenants_file(tmp_path, {"alice": T}), "--no-8123", func="render_nginx"
    )
    assert result.returncode == 0, result
    assert "listen 443;" in result.stdout
    assert "listen 8123;" not in result.stdout


def _render(out: Path) -> dict[str, bytes]:
    result = _bash("--render", out.as_posix(), EXAMPLE.as_posix())
    assert result.returncode == 0, result
    return {
        p.relative_to(out).as_posix(): p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()
    }


def test_render_idempotent(tmp_path):
    out = tmp_path / "out"
    first = _render(out)
    second = _render(out)
    assert first == second
    assert set(first) == {
        "sshd_match.conf",
        "sshd_global.conf",
        "nginx.conf",
        "authorized_keys/alice",
        "authorized_keys/bob",
    }


def test_render_rejects_invalid(tmp_path):
    out = tmp_path / "out"
    result = _bash(
        "--render", out.as_posix(), _tenants_file(tmp_path, {"Bad_Name": T})
    )
    assert result.returncode == 1
    assert "tenant Bad_Name" in result.stderr
    assert not out.exists()


def test_main_requires_root_without_render():
    if os.name != "nt" and os.geteuid() == 0:
        pytest.skip("running as root")
    result = _bash(EXAMPLE.as_posix())
    assert result.returncode == 1
    assert "root" in result.stderr


@pytest.mark.parametrize("fixture", ["sshd_config_legacy_unmarked", "sshd_config_legacy_marked"])
def test_strip_legacy_both_forms(tmp_path, fixture):
    target = tmp_path / "sshd_config"
    shutil.copyfile(FIXTURES / fixture, target)
    result = _bash(target.as_posix(), func="strip_legacy_sshd")
    assert result.returncode == 0, result
    text = target.read_bytes().decode()
    assert "ha-tunnel" not in text
    assert "GatewayPorts yes" not in text
    assert "BEGIN ha-relay" not in text
    lines = text.splitlines()
    for line in UBUNTU_LINES:
        assert line in lines


def test_write_sshd_match_is_stable(tmp_path):
    """write_sshd_match twice gives the same file (no growing blank lines)."""
    target = tmp_path / "sshd_config"
    shutil.copyfile(FIXTURES / "sshd_config_legacy_marked", target)
    tenants = _tenants_file(tmp_path, {"alice": T})
    code = f'{PRELUDE}source {SCRIPT}; for i in 1 2; do write_sshd_match "$1" "$2"; cp "$1" "$1.$i"; done'
    result = _run(code, target.as_posix(), tenants)
    assert result.returncode == 0, result
    first = (tmp_path / "sshd_config.1").read_bytes()
    assert first == (tmp_path / "sshd_config.2").read_bytes()
    assert first.count(b"# BEGIN ha-relay") == 1
    assert b"Match User relay-alice" in first
    assert b"ha-tunnel" not in first
    assert b"Include /etc/ssh/sshd_config.d/*.conf" in first


# Body of the legacy "Match User ha-tunnel" section, as found in each fixture.
LEGACY_BODIES = {
    "sshd_config_legacy_unmarked": [
        "    PasswordAuthentication yes",
        "    AllowTcpForwarding remote",
        "    PermitListen 8123",
        "    ForceCommand /usr/sbin/nologin",
        "    GatewayPorts yes",  # was a global line in this form
    ],
    "sshd_config_legacy_marked": [
        "    AuthenticationMethods publickey",
        "    GatewayPorts yes",
        "    AllowTcpForwarding remote",
        "    PermitListen 8123",
        "    PermitOpen none",
        "    AllowStreamLocalForwarding no",
        "    X11Forwarding no",
        "    AllowAgentForwarding no",
        "    PermitTTY no",
        "    ForceCommand /usr/sbin/nologin",
    ],
}

LOCKED_BODY = [
    "    AllowTcpForwarding no",
    "    PasswordAuthentication no",
    "    PermitTTY no",
    "    ForceCommand /usr/sbin/nologin",
]


def _legacy_section(text: str) -> list[str]:
    """Lines after "Match User ha-tunnel", up to the end marker."""
    lines = text.splitlines()
    start = lines.index("Match User ha-tunnel") + 1
    return lines[start : lines.index("# END ha-relay", start)]


def _write_runs(tmp_path: Path, fixture: str, *modes: str) -> list[str]:
    """Copy a fixture, run write_sshd_match once per mode, return each result."""
    target = tmp_path / "sshd_config"
    shutil.copyfile(FIXTURES / fixture, target)
    code = (
        f'{PRELUDE}source {SCRIPT}; f=$1 t=$2; shift 2; i=0; '
        'for mode in "$@"; do i=$((i+1)); write_sshd_match "$f" "$t" "$mode"; cp "$f" "$f.$i"; done'
    )
    tenants = _tenants_file(tmp_path, {"alice": T})
    result = _run(code, target.as_posix(), tenants, *modes)
    assert result.returncode == 0, result
    return [
        (tmp_path / f"sshd_config.{i}").read_bytes().decode() for i in range(1, len(modes) + 1)
    ]


@pytest.mark.parametrize("fixture", sorted(LEGACY_BODIES))
def test_extract_legacy_body(tmp_path, fixture):
    result = _bash((FIXTURES / fixture).as_posix(), func="extract_legacy_sshd")
    assert result.returncode == 0, result
    assert result.stdout.splitlines() == LEGACY_BODIES[fixture]


def test_extract_legacy_none_without_legacy(tmp_path):
    target = tmp_path / "sshd_config"
    shutil.copyfile(FIXTURES / "sshd_config_legacy_marked", target)
    assert _bash(target.as_posix(), func="strip_legacy_sshd").returncode == 0
    result = _bash(target.as_posix(), func="extract_legacy_sshd")
    assert result.returncode == 0, result
    assert result.stdout == ""


@pytest.mark.parametrize("fixture", sorted(LEGACY_BODIES))
def test_keep_preserves_legacy_body(tmp_path, fixture):
    first, second = _write_runs(tmp_path, fixture, "keep", "keep")
    assert first == second  # second run parses the section back from the block
    assert _legacy_section(first) == LEGACY_BODIES[fixture]
    lines = first.splitlines()
    assert "GatewayPorts yes" not in lines  # no global line left
    assert lines.count("# BEGIN ha-relay") == 1
    assert lines.index("Match User relay-alice") < lines.index("Match User ha-tunnel")
    for line in UBUNTU_LINES:
        assert line in lines


@pytest.mark.parametrize("fixture", sorted(LEGACY_BODIES))
def test_lock_replaces_legacy_body(tmp_path, fixture):
    locked, kept, relocked = _write_runs(tmp_path, fixture, "lock", "keep", "lock")
    assert _legacy_section(locked) == LOCKED_BODY
    assert "PermitListen 8123" not in locked
    assert "GatewayPorts yes" not in locked
    assert kept == locked  # a later run without --remove-legacy keeps it locked
    assert relocked == locked


def test_render_sshd_match_legacy_body(tmp_path):
    result = _bash(
        _tenants_file(tmp_path, {"alice": T}), "    PermitListen 8123", func="render_sshd_match"
    )
    assert result.returncode == 0, result
    lines = result.stdout.splitlines()
    assert lines[-3:] == ["Match User ha-tunnel", "    PermitListen 8123", "# END ha-relay"]


def test_require_tenants(tmp_path):
    empty = _tenants_file(tmp_path, {})
    assert _bash(empty, func="validate_tenants").returncode == 0
    result = _bash(empty, func="require_tenants")
    assert result.returncode == 1
    assert "no tenants" in result.stderr
    assert _bash(EXAMPLE.as_posix(), func="require_tenants").returncode == 0
    out = tmp_path / "out"
    assert _bash("--render", out.as_posix(), empty).returncode == 0


@pytest.mark.parametrize("fixture", sorted(LEGACY_BODIES))
def test_legacy_extractable_ok(fixture):
    result = _bash((FIXTURES / fixture).as_posix(), func="check_legacy_extractable")
    assert result.returncode == 0, result


def test_legacy_unparsable_refused(tmp_path):
    target = tmp_path / "sshd_config"
    shutil.copyfile(FIXTURES / "sshd_config_legacy_unparsable", target)
    before = target.read_bytes()
    result = _bash(target.as_posix(), func="check_legacy_extractable")
    assert result.returncode == 1
    assert "ha-tunnel" in result.stderr
    result = _bash(
        target.as_posix(), _tenants_file(tmp_path, {"alice": T}), "keep", func="write_sshd_match"
    )
    assert result.returncode == 1
    assert target.read_bytes() == before  # nothing written


@pytest.mark.parametrize(
    "mode,output,ok",
    [
        ("lock", "allowtcpforwarding no\nforcecommand /usr/sbin/nologin\npermittty no", True),
        ("lock", "allowtcpforwarding remote\nforcecommand /usr/sbin/nologin", False),
        ("lock", "allowtcpforwarding no\npermittty no", False),
        ("keep", "gatewayports yes\npermitlisten 8123", True),
        ("keep", "permitlisten localhost:8123", True),
        ("keep", "permitlisten 127.0.0.1:18123 8123", True),
        ("keep", "permitlisten any", True),
        ("keep", "permitlisten 127.0.0.1:18123", False),
        ("keep", "permitlisten 81234", False),
        ("keep", "permitlisten none", False),
    ],
)
def test_check_legacy_effective(tmp_path, mode, output, ok):
    path = tmp_path / "sshd-T.txt"
    path.write_bytes(output.encode() + b"\n")
    code = f'{PRELUDE}source {SCRIPT}; check_legacy_effective "$1" < "$2"'
    result = _run(code, mode, path.as_posix())
    assert result.returncode == (0 if ok else 1), result
    if not ok:
        assert "ha-tunnel" in result.stderr


def test_restore_configs_atomic(tmp_path):
    present = tmp_path / "sshd_config"
    present.write_bytes(b"new\n")
    (tmp_path / "sshd_config.bak.1").write_bytes(b"old\n")
    other = tmp_path / "nginx.conf"
    other.write_bytes(b"new\n")  # its backup is missing: the copy fails
    created = tmp_path / "ha-relay.conf"
    created.write_bytes(b"new\n")
    code = (
        f'{PRELUDE}source {SCRIPT}; BACKUPS=("$1.bak.1" "$2.bak.1"); CREATED=("$3"); '
        "restore_configs"
    )
    result = _run(code, present.as_posix(), other.as_posix(), created.as_posix())
    assert result.returncode == 0, result
    assert present.read_bytes() == b"old\n"
    assert other.read_bytes() == b"new\n"  # left in place, not removed
    assert "could not restore" in result.stderr
    assert not created.exists()
    assert not list(tmp_path.glob("*.restore.*"))


def test_bare_gatewayports_refused(tmp_path):
    """A "GatewayPorts yes" with no ha-tunnel section after it is not cut."""
    target = tmp_path / "sshd_config"
    shutil.copyfile(FIXTURES / "sshd_config_bare_gatewayports", target)
    before = target.read_bytes()
    for func in ("check_legacy_tail", "check_legacy_extractable", "strip_legacy_sshd"):
        result = _bash(target.as_posix(), func=func)
        assert result.returncode == 1, (func, result)
        assert "GatewayPorts yes" in result.stderr
    for mode in ("none", "keep", "lock"):
        result = _bash(
            target.as_posix(), _tenants_file(tmp_path, {"alice": T}), mode, func="write_sshd_match"
        )
        assert result.returncode == 1, (mode, result)
    assert target.read_bytes() == before  # nothing written


@pytest.mark.parametrize("fixture", [*sorted(LEGACY_BODIES), "sshd_config_legacy_unparsable"])
def test_legacy_tail_ok(fixture):
    result = _bash((FIXTURES / fixture).as_posix(), func="check_legacy_tail")
    assert result.returncode == 0, result


# Realistic "sshd -T" output (OpenSSH 8.9) for the admin user: unset options
# are printed with their default, e.g. "forcecommand none".
ADMIN_SSHD_T = """\
passwordauthentication no
gatewayports no
allowtcpforwarding yes
permitopen any
permitlisten any
forcecommand none
"""


def _verify_sshd(tmp_path, admin_output: str, admin: str | None = None):
    (tmp_path / "admin.txt").write_bytes(admin_output.encode())
    (tmp_path / "tenant.txt").write_bytes(
        b"passwordauthentication no\nforcecommand /usr/sbin/nologin\npermitlisten 127.0.0.1:18123\n"
    )
    env = f"export RELAY_ADMIN_USER={admin}; " if admin else ""
    code = (
        f'{PRELUDE}{env}source {SCRIPT}; d=$1; '
        'sshd() { case "$*" in *"user=relay-"*) cat "$d/tenant.txt" ;; *) echo "$*" >"$d/args"; cat "$d/admin.txt" ;; esac; }; '
        'verify_sshd "$2"'
    )
    return _run(code, tmp_path.as_posix(), _tenants_file(tmp_path, {"alice": T}))


def test_verify_sshd_forcecommand_none_ok(tmp_path):
    result = _verify_sshd(tmp_path, ADMIN_SSHD_T)
    assert result.returncode == 0, result
    assert "user=ubuntu," in (tmp_path / "args").read_text()


def test_verify_sshd_admin_forcecommand_refused(tmp_path):
    output = ADMIN_SSHD_T.replace("forcecommand none", "forcecommand /usr/sbin/nologin")
    result = _verify_sshd(tmp_path, output)
    assert result.returncode == 1
    assert "ForceCommand" in result.stderr


def test_verify_sshd_admin_password_refused(tmp_path):
    output = ADMIN_SSHD_T.replace("passwordauthentication no", "passwordauthentication yes")
    result = _verify_sshd(tmp_path, output)
    assert result.returncode == 1
    assert "passwordauthentication" in result.stderr


def test_verify_sshd_admin_user_override(tmp_path):
    result = _verify_sshd(tmp_path, ADMIN_SSHD_T, admin="opc")
    assert result.returncode == 0, result
    assert "user=opc," in (tmp_path / "args").read_text()
    assert "(opc)" in _verify_sshd(tmp_path, "forcecommand x\n", admin="opc").stderr


def test_render_sshd_match_client_alive(tmp_path):
    result = _bash(_tenants_file(tmp_path, {"alice": T}), func="render_sshd_match")
    assert result.returncode == 0, result
    lines = [line.strip() for line in result.stdout.splitlines()]
    group = lines.index("Match Group ha-relay")
    first_user = lines.index("Match User relay-alice")
    assert group < lines.index("ClientAliveInterval 15") < first_user
    assert group < lines.index("ClientAliveCountMax 3") < first_user


def test_render_nginx_proxy_timeout(tmp_path):
    result = _bash(_tenants_file(tmp_path, {"alice": T}), func="render_nginx")
    assert result.returncode == 0, result
    assert "    proxy_timeout 24h;" in result.stdout.splitlines()


def test_prune_backups_keeps_five(tmp_path):
    conf = tmp_path / "sshd_config"
    conf.write_bytes(b"x\n")
    stamps = [f"2026112000000{i}" for i in range(8)]
    for stamp in stamps:
        (tmp_path / f"sshd_config.bak.{stamp}").write_bytes(b"old\n")
    other = tmp_path / "nginx.conf"  # no backup at all: nothing to do
    result = _bash(conf.as_posix(), other.as_posix(), func="prune_backups")
    assert result.returncode == 0, result
    left = sorted(p.name for p in tmp_path.glob("sshd_config.bak.*"))
    assert left == [f"sshd_config.bak.{s}" for s in stamps[-5:]]
    assert conf.exists()


def test_sync_users_defers_removals(tmp_path):
    """sync_users only lists stale users; remove_stale_users deletes them."""
    code = (
        f'{PRELUDE}source {SCRIPT}; '
        'getent() { case "$1" in group) echo "ha-relay:x:999:relay-alice,relay-old" ;; esac; }; '
        'groupadd() { :; }; ensure_user() { echo "ensure $1"; }; '
        'remove_user() { echo "remove $1"; }; '
        'sync_users "$1" /stage; echo "stale ${STALE_USERS[*]}"; remove_stale_users'
    )
    result = _run(code, _tenants_file(tmp_path, {"alice": T}))
    assert result.returncode == 0, result
    assert result.stdout.splitlines() == ["ensure alice", "stale relay-old", "remove relay-old"]


def test_on_exit_ignores_signals_during_rollback():
    text = (ROOT / SCRIPT).read_text()
    body = text[text.index("on_exit() {"):]
    lines = body.splitlines()
    assert lines[1].strip() == "local rc=$?"
    assert "trap '' INT TERM HUP" in [line.strip() for line in lines[2:4]]
