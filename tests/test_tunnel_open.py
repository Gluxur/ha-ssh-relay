"""Opening the key-only reverse tunnel (no Home Assistant needed)."""

from __future__ import annotations

import asyncio
import socket

import asyncssh
import pytest

from conftest import load_tunnel

tunnel = load_tunnel()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _config(relay_server, private_key: str, remote_port: int, local_port: int = 1, **kw):
    return tunnel.RelayConfig(
        host="127.0.0.1",
        ssh_port=relay_server.port,
        username="tunnel",
        private_key=private_key,
        remote_port=remote_port,
        local_host="127.0.0.1",
        local_port=local_port,
        **kw,
    )


async def _close(conn) -> None:
    conn.close()
    await conn.wait_closed()


def test_generate_keypair_format():
    priv, pub = tunnel.generate_keypair()
    assert priv.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert pub.startswith("ssh-ed25519 ") and pub.endswith(" ha-ssh-relay")
    assert tunnel.fingerprint(pub).startswith("SHA256:")


async def test_open_tunnel_forwards_bytes(relay_server, echo_server):
    priv, pub = tunnel.generate_keypair()
    relay_server.authorized.append(pub)
    port = _free_port()
    relay_server.allowed_listens = {("127.0.0.1", port)}

    conn, listener, host_key = await tunnel.open_tunnel(
        _config(relay_server, priv, port, echo_server.port)
    )
    try:
        assert host_key == relay_server.host_key_public
        assert relay_server.auth_attempts >= 1
        assert relay_server.active_listens == {("127.0.0.1", port)}

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"ping")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(4), 5) == b"ping"
        writer.close()
        await writer.wait_closed()
    finally:
        listener.close()
        await _close(conn)


async def test_open_tunnel_with_matching_pinned_key(relay_server):
    priv, pub = tunnel.generate_keypair()
    relay_server.authorized.append(pub)
    port = _free_port()
    relay_server.allowed_listens = {("127.0.0.1", port)}

    conn, _listener, host_key = await tunnel.open_tunnel(
        _config(relay_server, priv, port, host_key=relay_server.host_key_public)
    )
    await _close(conn)
    assert host_key == relay_server.host_key_public


async def test_pinned_key_mismatch_raises(relay_server):
    priv, pub = tunnel.generate_keypair()
    relay_server.authorized.append(pub)
    port = _free_port()
    relay_server.allowed_listens = {("127.0.0.1", port)}
    _other_priv, other_pub = tunnel.generate_keypair()

    with pytest.raises(tunnel.HostKeyMismatch) as exc_info:
        await tunnel.open_tunnel(_config(relay_server, priv, port, host_key=other_pub))

    assert exc_info.value.expected == tunnel.fingerprint(other_pub)
    assert exc_info.value.actual == tunnel.fingerprint(relay_server.host_key_public)
    assert relay_server.auth_attempts == 0
    assert relay_server.active_listens == set()


async def test_unknown_client_key_permission_denied(relay_server):
    priv, _pub = tunnel.generate_keypair()
    _other_priv, other_pub = tunnel.generate_keypair()
    relay_server.authorized.append(other_pub)
    port = _free_port()
    relay_server.allowed_listens = {("127.0.0.1", port)}

    with pytest.raises(asyncssh.PermissionDenied):
        await tunnel.open_tunnel(_config(relay_server, priv, port))


async def test_forward_refused(relay_server):
    priv, pub = tunnel.generate_keypair()
    relay_server.authorized.append(pub)

    with pytest.raises(tunnel.ForwardRefused):
        await tunnel.open_tunnel(_config(relay_server, priv, _free_port()))
    assert relay_server.active_listens == set()
    # The client closed its side: the server must not keep a connection around.
    async with asyncio.timeout(5):
        while relay_server.open_connections:
            await asyncio.sleep(0.01)
