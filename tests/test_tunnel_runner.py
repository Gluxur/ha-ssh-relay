"""TunnelRunner reconnect loop: states, pinning, waiting and backoff."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from datetime import timezone
import socket

from conftest import load_tunnel

tunnel = load_tunnel()

FAST = {
    "waiting_delay": 0.05,
    "auth_retry_delay": 0.05,
    "forward_retry_delay": 0.05,
    "max_backoff": 0.05,
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Recorder:
    """Collects on_state / on_pinned calls and waits on them without fixed sleeps."""

    def __init__(self) -> None:
        self.states: list[str] = []
        self.pinned: list[str] = []

    def on_state(self, state: str) -> None:
        self.states.append(state)

    def on_pinned(self, key: str) -> None:
        self.pinned.append(key)

    async def wait_state(self, state: str, *, after: int = 0, timeout: float = 5) -> int:
        """Return the index of the first `state` recorded at or after `after`."""
        async with asyncio.timeout(timeout):
            while True:
                with contextlib.suppress(ValueError):
                    return self.states.index(state, after)
                await asyncio.sleep(0.01)


async def wait_until(predicate, timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def _setup(relay_server, local_port: int = 1, *, authorize: bool = True, allow: bool = True):
    priv, pub = tunnel.generate_keypair()
    if authorize:
        relay_server.authorized.append(pub)
    port = _free_port()
    if allow:
        relay_server.allowed_listens = {("127.0.0.1", port)}
    cfg = tunnel.RelayConfig(
        host="127.0.0.1",
        ssh_port=relay_server.port,
        username="tunnel",
        private_key=priv,
        remote_port=port,
        local_host="127.0.0.1",
        local_port=local_port,
    )
    return cfg, pub, port


@contextlib.asynccontextmanager
async def running(cfg, rec: Recorder, **kw):
    runner = tunnel.TunnelRunner(cfg, rec.on_state, rec.on_pinned, **{**FAST, **kw})
    task = asyncio.create_task(runner.run())
    try:
        yield runner, task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)


async def test_connects_and_pins_once(relay_server, echo_server):
    cfg, _pub, port = _setup(relay_server, echo_server.port)
    rec = Recorder()
    async with running(cfg, rec) as (runner, _task):
        await rec.wait_state("connected")
        assert rec.states[:2] == ["connecting", "connected"]
        assert rec.pinned == [relay_server.host_key_public]
        assert runner.host_key == relay_server.host_key_public
        assert runner.state == tunnel.STATE_CONNECTED
        assert runner.connected_at is not None
        assert runner.connected_at.tzinfo == timezone.utc
        assert runner.last_error is None
        assert relay_server.active_listens == {("127.0.0.1", port)}


async def test_reconnects_after_server_restart(relay_server, echo_server):
    cfg, _pub, port = _setup(relay_server, echo_server.port)
    rec = Recorder()
    async with running(cfg, rec) as (runner, _task):
        first = await rec.wait_state("connected")
        await relay_server.restart()
        await rec.wait_state("connected", after=first + 1)
        assert "reconnecting" in rec.states[first + 1 :]
        assert runner.reconnect_count >= 1
        assert rec.pinned == [relay_server.host_key_public]
        await wait_until(lambda: relay_server.active_listens == {("127.0.0.1", port)})


async def test_host_key_change_blocks(relay_server, echo_server):
    cfg, _pub, _port = _setup(relay_server, echo_server.port)
    rec = Recorder()
    async with running(cfg, rec) as (runner, _task):
        first = await rec.wait_state("connected")
        pinned = relay_server.host_key_public
        await relay_server.restart(new_host_key=True)
        mismatch = await rec.wait_state("host_key_mismatch", after=first + 1)
        assert relay_server.active_listens == set()
        assert runner.host_key == pinned
        assert "expected" in runner.last_error and "got" in runner.last_error
        assert tunnel.fingerprint(pinned) in runner.last_error
        assert runner.last_mismatch is not None
        assert runner.last_mismatch.actual_key == relay_server.host_key_public
        assert runner.last_mismatch.actual == tunnel.fingerprint(relay_server.host_key_public)
        # The retry still uses the pinned key: it fails the same way, never connects.
        await rec.wait_state("host_key_mismatch", after=mismatch + 1)
        assert "connected" not in rec.states[first + 1 :]
        assert relay_server.active_listens == set()
        assert relay_server.auth_attempts == 1
        assert rec.pinned == [pinned]


async def test_waiting_then_pins(relay_server, echo_server):
    cfg, pub, _port = _setup(relay_server, echo_server.port, authorize=False)
    rec = Recorder()
    async with running(cfg, rec) as (runner, _task):
        await rec.wait_state("waiting_for_relay")
        assert rec.pinned == []
        assert runner.host_key is None
        relay_server.authorized.append(pub)
        await rec.wait_state("connected")
        assert rec.pinned == [relay_server.host_key_public]


async def test_permission_denied_after_success_is_error(relay_server, echo_server):
    cfg, pub, _port = _setup(relay_server, echo_server.port)
    rec = Recorder()
    async with running(cfg, rec) as (_runner, _task):
        first = await rec.wait_state("connected")
        relay_server.authorized.remove(pub)
        await relay_server.restart()
        await rec.wait_state("error", after=first + 1)
        assert "waiting_for_relay" not in rec.states


async def test_permission_denied_with_configured_key_is_error(relay_server):
    cfg, _pub, _port = _setup(relay_server, authorize=False)
    cfg = dataclasses.replace(cfg, host_key=relay_server.host_key_public)
    rec = Recorder()
    async with running(cfg, rec):
        await rec.wait_state("error")
        assert "waiting_for_relay" not in rec.states
        assert rec.pinned == []


async def test_forward_refused_backs_off(relay_server):
    cfg, _pub, _port = _setup(relay_server, allow=False)
    rec = Recorder()
    # A refused forward waits forward_retry_delay, not auth_retry_delay.
    async with running(
        cfg, rec, auth_retry_delay=60, forward_retry_delay=0.1
    ) as (runner, _task):
        first = await rec.wait_state("error")
        await rec.wait_state("error", after=first + 1, timeout=2)
        start = relay_server.auth_attempts
        await asyncio.sleep(0.3)
        assert relay_server.auth_attempts - start <= 4
        assert runner.state in (tunnel.STATE_ERROR, tunnel.STATE_CONNECTING)
        assert relay_server.active_listens == set()


async def test_cancel_sets_disconnected(relay_server, echo_server):
    cfg, _pub, _port = _setup(relay_server, echo_server.port)
    rec = Recorder()
    runner = tunnel.TunnelRunner(cfg, rec.on_state, rec.on_pinned, **FAST)
    task = asyncio.create_task(runner.run())
    await rec.wait_state("connected")
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert task.done()
    assert runner.state == tunnel.STATE_DISCONNECTED
    assert rec.states[-1] == "disconnected"
    assert runner.connected_at is None
    await wait_until(lambda: relay_server.open_connections == 0)
    assert relay_server.active_listens == set()


async def test_mismatch_unknown_key_text(relay_server, monkeypatch):
    """A failed probe reports "unknown", never "None"."""
    cfg, _pub, _port = _setup(relay_server)
    _other_priv, other_pub = tunnel.generate_keypair()
    cfg = dataclasses.replace(cfg, host_key=other_pub)

    async def no_key(_cfg):
        return None

    monkeypatch.setattr(tunnel, "_probe_host_key", no_key)
    rec = Recorder()
    async with running(cfg, rec) as (runner, _task):
        await rec.wait_state("host_key_mismatch")
        assert runner.last_mismatch.actual is None
        assert runner.last_mismatch.actual_key is None
        assert "None" not in runner.last_error
        assert runner.last_error.endswith("got unknown")
        assert "got unknown" in str(runner.last_mismatch)
