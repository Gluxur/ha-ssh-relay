"""Tunnel status, uptime and reconnect count sensors."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from . import SshRelayConfigEntry
from .const import CONF_RELAY_HOST, CONF_REMOTE_PORT, CONF_TENANT
from .coordinator import RelayCoordinator, device_info
from .tunnel import (
    STATE_CONNECTED,
    STATE_CONNECTING,
    STATE_DISCONNECTED,
    STATE_ERROR,
    STATE_HOST_KEY_MISMATCH,
    STATE_RECONNECTING,
    STATE_WAITING,
)

STATES = [
    STATE_CONNECTING,
    STATE_CONNECTED,
    STATE_RECONNECTING,
    STATE_DISCONNECTED,
    STATE_ERROR,
    STATE_WAITING,
    STATE_HOST_KEY_MISMATCH,
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SshRelayConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([
        SshRelayStatusSensor(coordinator, entry),
        SshRelayUptimeSensor(coordinator, entry),
        SshRelayReconnectCountSensor(coordinator, entry),
    ])


class _SshRelayBaseSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: RelayCoordinator, entry: SshRelayConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._unsub_coordinator: Callable[[], None] | None = None
        self._attr_unique_id = f"{entry.entry_id}_{self._attr_translation_key}"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        self._unsub_coordinator = self._coordinator.add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_coordinator:
            self._unsub_coordinator()

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class SshRelayStatusSensor(_SshRelayBaseSensor):
    _attr_translation_key = "status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATES

    @property
    def native_value(self) -> str:
        return self._coordinator.state

    @property
    def icon(self) -> str:
        if self._coordinator.state == STATE_CONNECTED:
            return "mdi:tunnel"
        return "mdi:tunnel-outline"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last = self._coordinator.last_connected_at
        return {
            "relay_host": self._entry.data[CONF_RELAY_HOST],
            "tenant": self._entry.data[CONF_TENANT],
            "remote_port": self._entry.data[CONF_REMOTE_PORT],
            "host_key_fingerprint": self._coordinator.host_key_fingerprint,
            "last_error": self._coordinator.last_error,
            "last_connected_at": last.isoformat() if last else None,
        }


class SshRelayUptimeSensor(_SshRelayBaseSensor):
    _attr_translation_key = "uptime"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer-outline"

    def __init__(self, coordinator: RelayCoordinator, entry: SshRelayConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._unsub_timer: Callable[[], None] | None = None

    @callback
    def _async_update_uptime(self, _now: object) -> None:
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub_timer = async_track_time_interval(
            self.hass, self._async_update_uptime, timedelta(seconds=5)
        )

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None

    @property
    def native_value(self) -> int | None:
        connected_at = self._coordinator.connected_at
        if self._coordinator.state != STATE_CONNECTED or connected_at is None:
            return None
        return int((dt_util.utcnow() - connected_at).total_seconds())


class SshRelayReconnectCountSensor(_SshRelayBaseSensor):
    _attr_translation_key = "reconnect_count"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:refresh"

    @property
    def native_value(self) -> int:
        return self._coordinator.reconnect_count
