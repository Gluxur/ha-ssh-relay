"""Runs the reverse tunnel of one config entry and reports its state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    CONF_HOST_KEY,
    CONF_KEEPALIVE,
    CONF_LOCAL_HOST,
    CONF_LOCAL_PORT,
    CONF_PRIVATE_KEY,
    CONF_RELAY_HOST,
    CONF_REMOTE_PORT,
    CONF_SSH_PORT,
    CONF_TENANT,
    DEFAULT_KEEPALIVE,
    DOMAIN,
    ISSUE_HOST_KEY_CHANGED,
    ISSUE_HOST_KEY_CHANGED_UNKNOWN,
    STOP_TIMEOUT,
    issue_id,
    relay_username,
)
from .tunnel import (
    STATE_CONNECTED,
    STATE_HOST_KEY_MISMATCH,
    RelayConfig,
    TunnelRunner,
    fingerprint,
)


def relay_config(data: dict[str, Any]) -> RelayConfig:
    """Build the tunnel configuration from an entry's data."""
    return RelayConfig(
        host=data[CONF_RELAY_HOST],
        ssh_port=data[CONF_SSH_PORT],
        username=relay_username(data[CONF_TENANT]),
        private_key=data[CONF_PRIVATE_KEY],
        remote_port=data[CONF_REMOTE_PORT],
        local_host=data[CONF_LOCAL_HOST],
        local_port=data[CONF_LOCAL_PORT],
        keepalive=data.get(CONF_KEEPALIVE, DEFAULT_KEEPALIVE),
        host_key=data.get(CONF_HOST_KEY),
    )


def device_info(entry: ConfigEntry) -> DeviceInfo:
    """The device shared by the entities of an entry."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"SSH Relay → {entry.data[CONF_RELAY_HOST]} ({entry.data[CONF_TENANT]})",
        manufacturer="ha-ssh-relay",
        model="Reverse SSH tunnel",
    )


def safe_fingerprint(key: str | None) -> str | None:
    """Fingerprint of an OpenSSH public key, None if absent or unreadable."""
    if not key:
        return None
    try:
        return fingerprint(key)
    except Exception:  # noqa: BLE001 - a bad stored key must not break entities
        return None


class RelayCoordinator:
    """Owns the TunnelRunner of a config entry and notifies entities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        # Options seen at setup; the update listener reloads only when they change.
        self.options = dict(entry.options)
        self.last_connected_at: datetime | None = None
        self._listeners: list[Callable[[], None]] = []
        self._task: asyncio.Task | None = None
        self._runner = TunnelRunner(
            relay_config(dict(entry.data)), self._on_state, self._on_pinned
        )

    # State read by the entities.

    @property
    def state(self) -> str:
        return self._runner.state

    @property
    def reconnect_count(self) -> int:
        return self._runner.reconnect_count

    @property
    def connected_at(self) -> datetime | None:
        return self._runner.connected_at

    @property
    def last_error(self) -> str | None:
        return self._runner.last_error

    @property
    def host_key_fingerprint(self) -> str | None:
        return safe_fingerprint(self._runner.host_key)

    # Listeners.

    def add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(update)

        def _remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(update)

        return _remove

    def _notify_listeners(self) -> None:
        for update in list(self._listeners):
            update()

    # Runner callbacks, called from the event loop.

    @callback
    def _on_state(self, state: str) -> None:
        if state == STATE_CONNECTED:
            self.last_connected_at = self._runner.connected_at
            ir.async_delete_issue(self.hass, DOMAIN, issue_id(self.entry.entry_id))
        elif state == STATE_HOST_KEY_MISMATCH:
            self._raise_host_key_issue()
        self._notify_listeners()

    @callback
    def _on_pinned(self, key: str) -> None:
        # Data only: the update listener does not reload for a data change.
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_HOST_KEY: key}
        )

    @callback
    def _raise_host_key_issue(self) -> None:
        data = self.entry.data
        expected = safe_fingerprint(data.get(CONF_HOST_KEY)) or "?"
        mismatch = self._runner.last_mismatch
        actual_key = mismatch.actual_key if mismatch is not None else None
        actual = safe_fingerprint(actual_key)
        placeholders = {
            "host": f"{data[CONF_RELAY_HOST]}:{data[CONF_SSH_PORT]}",
            "expected": expected,
        }
        if actual is not None:
            placeholders["actual"] = actual
        else:
            actual_key = None
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id(self.entry.entry_id),
            is_fixable=True,
            severity=ir.IssueSeverity.ERROR,
            # Without the presented key, the translation says "unknown".
            translation_key=(
                ISSUE_HOST_KEY_CHANGED if actual else ISSUE_HOST_KEY_CHANGED_UNKNOWN
            ),
            translation_placeholders=placeholders,
            # The repair pins exactly the key shown (None: re-pin on next connect).
            data={"entry_id": self.entry.entry_id, "host_key": actual_key},
        )

    # Lifecycle.

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = self.hass.async_create_background_task(
            self._runner.run(), f"{DOMAIN} tunnel {self.entry.title}"
        )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        # run() re-raises CancelledError after reporting `disconnected`.
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, STOP_TIMEOUT)
