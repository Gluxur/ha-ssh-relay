"""Shared fixtures: an in-process asyncssh relay server and a local echo server."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys

import asyncssh
import pytest

_TUNNEL_PATH = Path(__file__).parents[1] / "custom_components" / "ssh_relay" / "tunnel.py"


def load_tunnel():
    """Load tunnel.py by file path (Home Assistant is not installed in tests)."""
    if "ssh_relay_tunnel" in sys.modules:
        return sys.modules["ssh_relay_tunnel"]
    spec = importlib.util.spec_from_file_location("ssh_relay_tunnel", _TUNNEL_PATH)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so @dataclass can resolve the module.
    sys.modules["ssh_relay_tunnel"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules["ssh_relay_tunnel"]
        raise
    return module


def _public_openssh(key: asyncssh.SSHKey) -> str:
    return key.export_public_key("openssh").decode().strip()


class RelayServer:
    """A fake relay VM: key-only auth, remote forwards limited to `allowed_listens`."""

    def __init__(self) -> None:
        self.port = 0
        self.host_key = asyncssh.generate_private_key("ssh-ed25519")
        self.host_key_public = _public_openssh(self.host_key)
        # OpenSSH public key strings accepted for authentication.
        self.authorized: list[str] = []
        # (listen_host, listen_port) pairs a client may open with -R.
        self.allowed_listens: set[tuple[str, int]] = set()
        # Remote-forward listeners currently open on the server.
        self.active_listens: set[tuple[str, int]] = set()
        # Number of connections that started user authentication.
        self.auth_attempts = 0
        self._acceptor: asyncssh.SSHAcceptor | None = None
        self._conns: set[asyncssh.SSHServerConnection] = set()

    def _make_server(self) -> asyncssh.SSHServer:
        relay = self

        class _Server(asyncssh.SSHServer):
            def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
                self._conn = conn
                relay._conns.add(conn)

            def connection_lost(self, exc: Exception | None) -> None:
                relay._conns.discard(self._conn)

            def begin_auth(self, username: str) -> bool:
                relay.auth_attempts += 1
                return True

            def password_auth_supported(self) -> bool:
                return False

            def public_key_auth_supported(self) -> bool:
                return True

            def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
                return any(
                    asyncssh.import_public_key(allowed).public_data == key.public_data
                    for allowed in relay.authorized
                )

            def server_requested(self, listen_host: str, listen_port: int):
                if (listen_host, listen_port) not in relay.allowed_listens:
                    return False
                return relay._open_listen(self._conn, listen_host, listen_port)

        return _Server()

    async def _open_listen(
        self, conn: asyncssh.SSHServerConnection, listen_host: str, listen_port: int
    ) -> asyncssh.SSHListener:
        # Same thing asyncssh does when server_requested returns True, plus tracking.
        listener = await conn.forward_local_port(
            listen_host, listen_port, listen_host, listen_port
        )
        key = (listen_host, listen_port)
        self.active_listens.add(key)
        original_close = listener.close

        def close() -> None:
            self.active_listens.discard(key)
            original_close()

        listener.close = close
        return listener

    async def start(self) -> None:
        self._acceptor = await asyncssh.create_server(
            self._make_server,
            "127.0.0.1",
            self.port,
            server_host_keys=[self.host_key],
            config=None,
        )
        self.port = self._acceptor.get_port()

    async def stop(self) -> None:
        if self._acceptor is not None:
            self._acceptor.close()
        for conn in list(self._conns):
            conn.close()
        for conn in list(self._conns):
            await conn.wait_closed()
        if self._acceptor is not None:
            await self._acceptor.wait_closed()
            self._acceptor = None

    async def restart(self, new_host_key: bool = False) -> None:
        """Drop every connection and listen again on the same port."""
        await self.stop()
        if new_host_key:
            self.host_key = asyncssh.generate_private_key("ssh-ed25519")
            self.host_key_public = _public_openssh(self.host_key)
        await self.start()


@pytest.fixture
async def relay_server():
    server = RelayServer()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def echo_server():
    """Local TCP server echoing what it receives (stands in for Home Assistant)."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    server.port = server.sockets[0].getsockname()[1]
    yield server
    server.close()
    await server.wait_closed()
