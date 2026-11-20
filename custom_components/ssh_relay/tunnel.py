"""Reverse SSH tunnel core: keys and key-only tunnel opening (no Home Assistant)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging

import asyncssh

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 45
FORWARD_TIMEOUT = 30
KEEPALIVE_COUNT_MAX = 3
HOST_KEY_ALGS = ["ssh-ed25519"]
REMOTE_LISTEN_HOST = "127.0.0.1"


@dataclass(frozen=True)
class RelayConfig:
    """Everything needed to open the reverse tunnel."""

    host: str
    ssh_port: int
    username: str
    private_key: str
    remote_port: int
    local_host: str
    local_port: int
    keepalive: int = 30
    # Pinned relay host public key (OpenSSH format); None = accept and report.
    host_key: str | None = None

    def __repr__(self) -> str:
        # Never expose the private key in logs or tracebacks.
        return (
            f"RelayConfig(host={self.host!r}, ssh_port={self.ssh_port}, "
            f"username={self.username!r}, remote_port={self.remote_port}, "
            f"local_host={self.local_host!r}, local_port={self.local_port}, "
            f"keepalive={self.keepalive}, host_key={self.host_key!r})"
        )


class HostKeyMismatch(Exception):
    """The relay presented a host key other than the pinned one."""

    def __init__(self, expected: str, actual: str | None) -> None:
        super().__init__(f"relay host key mismatch: expected {expected}, got {actual}")
        self.expected = expected
        self.actual = actual


class ForwardRefused(Exception):
    """The relay refused the remote port forward."""


def generate_keypair(comment: str = "ha-ssh-relay") -> tuple[str, str]:
    """Return (OpenSSH private key, `ssh-ed25519 AAAA… <comment>` public key)."""
    key = asyncssh.generate_private_key("ssh-ed25519", comment=comment)
    private = key.export_private_key("openssh").decode()
    public = key.export_public_key("openssh").decode().strip()
    return private, public


def fingerprint(public_key: str) -> str:
    """Return the `SHA256:…` fingerprint of an OpenSSH public key (as ssh-keygen -lf)."""
    return asyncssh.import_public_key(public_key).get_fingerprint("sha256")


def _export_public(key: asyncssh.SSHKey) -> str:
    return key.export_public_key("openssh").decode().strip()


async def _probe_fingerprint(cfg: RelayConfig) -> str | None:
    """Read the relay's host key fingerprint without authenticating."""
    try:
        key = await asyncio.wait_for(
            asyncssh.get_server_host_key(
                cfg.host,
                cfg.ssh_port,
                server_host_key_algs=HOST_KEY_ALGS,
                config=None,
            ),
            CONNECT_TIMEOUT,
        )
    except (OSError, asyncssh.Error, asyncio.TimeoutError) as err:
        _LOGGER.debug(
            "Host key probe of %s:%s failed: %s: %s",
            cfg.host, cfg.ssh_port, type(err).__name__, err,
        )
        return None
    return key.get_fingerprint("sha256") if key is not None else None


async def open_tunnel(
    cfg: RelayConfig,
) -> tuple[asyncssh.SSHClientConnection, asyncssh.SSHListener, str]:
    """Connect with the private key and open `-R 127.0.0.1:<remote_port>`.

    Returns the connection, the remote listener and the relay host public key
    seen during the handshake (OpenSSH format).
    """
    if cfg.host_key:
        known_hosts = ([asyncssh.import_public_key(cfg.host_key)], [], [])
    else:
        known_hosts = None

    try:
        conn = await asyncio.wait_for(
            asyncssh.connect(
                cfg.host,
                cfg.ssh_port,
                username=cfg.username,
                client_keys=[asyncssh.import_private_key(cfg.private_key)],
                known_hosts=known_hosts,
                server_host_key_algs=HOST_KEY_ALGS,
                keepalive_interval=cfg.keepalive,
                keepalive_count_max=KEEPALIVE_COUNT_MAX,
                login_timeout=CONNECT_TIMEOUT,
                # Only the configured key: no ~/.ssh/config, no agent.
                config=None,
                agent_path=None,
            ),
            CONNECT_TIMEOUT,
        )
    except asyncssh.HostKeyNotVerifiable as err:
        expected = fingerprint(cfg.host_key) if cfg.host_key else ""
        actual = await _probe_fingerprint(cfg)
        _LOGGER.warning(
            "Relay %s:%s host key mismatch: expected %s, got %s",
            cfg.host, cfg.ssh_port, expected, actual,
        )
        raise HostKeyMismatch(expected, actual) from err

    try:
        server_key = conn.get_server_host_key()
        host_key = _export_public(server_key) if server_key is not None else ""
        listener = await asyncio.wait_for(
            conn.forward_remote_port(
                REMOTE_LISTEN_HOST, cfg.remote_port, cfg.local_host, cfg.local_port
            ),
            FORWARD_TIMEOUT,
        )
    except asyncssh.ChannelListenError as err:
        conn.close()
        await conn.wait_closed()
        _LOGGER.warning(
            "Relay refused forward of %s:%s: %s: %s",
            REMOTE_LISTEN_HOST, cfg.remote_port, type(err).__name__, err,
        )
        raise ForwardRefused(str(err)) from err
    except BaseException:
        conn.close()
        raise

    _LOGGER.info(
        "Tunnel open: %s:%s on relay %s:%s -> %s:%s",
        REMOTE_LISTEN_HOST, cfg.remote_port, cfg.host, cfg.ssh_port,
        cfg.local_host, cfg.local_port,
    )
    return conn, listener, host_key
