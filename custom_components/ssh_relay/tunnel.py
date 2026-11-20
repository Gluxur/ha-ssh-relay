"""Reverse SSH tunnel core: keys and key-only tunnel opening (no Home Assistant)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
import logging

import asyncssh

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 45
FORWARD_TIMEOUT = 30
KEEPALIVE_COUNT_MAX = 3
HOST_KEY_ALGS = ["ssh-ed25519"]
REMOTE_LISTEN_HOST = "127.0.0.1"
CLOSE_TIMEOUT = 5

STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_RECONNECTING = "reconnecting"
STATE_DISCONNECTED = "disconnected"
STATE_ERROR = "error"
STATE_WAITING = "waiting_for_relay"
STATE_HOST_KEY_MISMATCH = "host_key_mismatch"


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

    def __init__(
        self, expected: str, actual: str | None, actual_key: str | None = None
    ) -> None:
        super().__init__(
            f"relay host key mismatch: expected {expected}, got {actual or 'unknown'}"
        )
        self.expected = expected
        # Fingerprint and OpenSSH public key of the key presented, None if the
        # probe could not read it.
        self.actual = actual
        self.actual_key = actual_key


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


async def _probe_host_key(cfg: RelayConfig) -> str | None:
    """Read the relay's host public key (OpenSSH format) without authenticating."""
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
    return _export_public(key) if key is not None else None


async def _close(conn: asyncssh.SSHClientConnection) -> None:
    """Close the connection and wait (bounded) for it to be fully closed."""
    conn.close()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(conn.wait_closed(), CLOSE_TIMEOUT)


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
                # Only the configured key: no ~/.ssh/config, no agent, no
                # GSSAPI, and public key authentication only.
                config=None,
                agent_path=None,
                gss_host=None,
                preferred_auth="publickey",
            ),
            CONNECT_TIMEOUT,
        )
    except asyncssh.HostKeyNotVerifiable as err:
        expected = fingerprint(cfg.host_key) if cfg.host_key else ""
        actual_key = await _probe_host_key(cfg)
        actual = fingerprint(actual_key) if actual_key else None
        _LOGGER.warning(
            "Relay %s:%s host key mismatch: expected %s, got %s",
            cfg.host, cfg.ssh_port, expected, actual or "unknown",
        )
        raise HostKeyMismatch(expected, actual, actual_key) from err

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
        await _close(conn)
        _LOGGER.warning(
            "Relay refused forward of %s:%s: %s: %s",
            REMOTE_LISTEN_HOST, cfg.remote_port, type(err).__name__, err,
        )
        raise ForwardRefused(str(err)) from err
    except asyncio.CancelledError:
        # Do not await anything while being cancelled.
        conn.close()
        raise
    except BaseException:
        await _close(conn)
        raise

    _LOGGER.info(
        "Tunnel open: %s:%s on relay %s:%s -> %s:%s",
        REMOTE_LISTEN_HOST, cfg.remote_port, cfg.host, cfg.ssh_port,
        cfg.local_host, cfg.local_port,
    )
    return conn, listener, host_key


class TunnelRunner:
    """Keep the reverse tunnel open until cancelled, reporting its state.

    `on_state(state)` is called on every state change. `on_pinned(key)` is
    called once, with the relay host public key, after the first successful
    connection made without a pinned key; that key is then used for every
    later attempt.
    """

    def __init__(
        self,
        cfg: RelayConfig,
        on_state: Callable[[str], None],
        on_pinned: Callable[[str], None],
        *,
        waiting_delay: float = 30,
        auth_retry_delay: float = 300,
        forward_retry_delay: float = 60,
        max_backoff: float = 60,
    ) -> None:
        self._cfg = cfg
        self._on_state = on_state
        self._on_pinned = on_pinned
        self._waiting_delay = waiting_delay
        self._auth_retry_delay = auth_retry_delay
        # Shorter than auth_retry_delay: a refused forward is usually the
        # previous session still holding the port on the relay.
        self._forward_retry_delay = forward_retry_delay
        self._max_backoff = max_backoff
        self._ever_connected = False
        self.state: str = STATE_DISCONNECTED
        self.reconnect_count: int = 0
        self.connected_at: datetime | None = None
        self.host_key: str | None = cfg.host_key
        self.last_error: str | None = None
        # Last host key mismatch seen, until the next successful connection.
        self.last_mismatch: HostKeyMismatch | None = None

    def _set_state(self, state: str) -> None:
        self.state = state
        self.connected_at = (
            datetime.now(timezone.utc) if state == STATE_CONNECTED else None
        )
        try:
            self._on_state(state)
        except Exception:  # noqa: BLE001 - a callback must not kill the loop
            _LOGGER.exception("on_state callback failed")

    def _pin(self, key: str) -> None:
        if self.host_key is not None or not key:
            return
        self.host_key = key
        self._cfg = dataclasses.replace(self._cfg, host_key=key)
        _LOGGER.info("Pinned relay host key %s", fingerprint(key))
        try:
            self._on_pinned(key)
        except Exception:  # noqa: BLE001 - a callback must not kill the loop
            _LOGGER.exception("on_pinned callback failed")

    def _next_backoff(self, backoff: float) -> float:
        return min(backoff * 2, self._max_backoff)

    async def run(self) -> None:
        """Loop until cancelled; the state is `disconnected` on exit."""
        initial_backoff = min(1, self._max_backoff)
        backoff = initial_backoff
        try:
            while True:
                self._set_state(STATE_CONNECTING)
                try:
                    conn, listener, seen_key = await open_tunnel(self._cfg)
                except HostKeyMismatch as err:
                    self.last_mismatch = err
                    self.last_error = (
                        f"expected {err.expected} got {err.actual or 'unknown'}"
                    )
                    state, delay = STATE_HOST_KEY_MISMATCH, self._auth_retry_delay
                except asyncssh.PermissionDenied as err:
                    self.last_error = f"permission denied: {err}"
                    if not self._ever_connected and self._cfg.host_key is None:
                        # Fresh install: the key is not on the relay yet.
                        state, delay = STATE_WAITING, self._waiting_delay
                    else:
                        _LOGGER.error(
                            "Relay %s:%s rejected the key for %s",
                            self._cfg.host, self._cfg.ssh_port, self._cfg.username,
                        )
                        state, delay = STATE_ERROR, self._auth_retry_delay
                except ForwardRefused as err:
                    self.last_error = f"forward refused: {err}"
                    state, delay = STATE_ERROR, self._forward_retry_delay
                except (TimeoutError, OSError, asyncssh.Error) as err:
                    self.last_error = f"{type(err).__name__}: {err}"
                    _LOGGER.warning("Relay connection failed: %s", self.last_error)
                    state, delay = STATE_RECONNECTING, backoff
                    backoff = self._next_backoff(backoff)
                except Exception as err:  # noqa: BLE001 - never die silently
                    self.last_error = f"{type(err).__name__}: {err}"
                    _LOGGER.exception("Unexpected relay tunnel error")
                    state, delay = STATE_RECONNECTING, backoff
                    backoff = self._next_backoff(backoff)
                else:
                    self._ever_connected = True
                    self.last_error = None
                    self.last_mismatch = None
                    self._pin(seen_key)
                    await self._hold(conn, listener)
                    backoff = initial_backoff
                    state, delay = STATE_RECONNECTING, backoff
                    backoff = self._next_backoff(backoff)

                self._set_state(state)
                self.reconnect_count += 1
                _LOGGER.debug(
                    "Retrying in %ss (attempt #%s)", delay, self.reconnect_count
                )
                await asyncio.sleep(delay)
        finally:
            self._set_state(STATE_DISCONNECTED)

    async def _hold(
        self, conn: asyncssh.SSHClientConnection, listener: asyncssh.SSHListener
    ) -> None:
        """Report `connected`, then wait for the listener or the connection to close."""
        waiters: set[asyncio.Future] = set()
        try:
            self._set_state(STATE_CONNECTED)
            waiters = {
                asyncio.ensure_future(listener.wait_closed()),
                asyncio.ensure_future(conn.wait_closed()),
            }
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            # Do not await anything while being cancelled.
            conn.close()
            raise
        finally:
            for waiter in waiters:
                waiter.cancel()
        _LOGGER.warning("Relay tunnel closed")
        await _close(conn)
