"""Switch enabling or disabling the tunnel."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SshRelayConfigEntry
from .const import CONF_ENABLED
from .coordinator import RelayCoordinator, device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SshRelayConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([SshRelaySwitch(entry.runtime_data, entry)])


class SshRelaySwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "enabled"
    _attr_should_poll = False

    def __init__(self, coordinator: RelayCoordinator, entry: SshRelayConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._unsub_coordinator: Callable[[], None] | None = None
        self._attr_unique_id = f"{entry.entry_id}_enabled"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        self._unsub_coordinator = self._coordinator.add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_coordinator:
            self._unsub_coordinator()

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._entry.options.get(CONF_ENABLED, True)

    async def async_turn_on(self, **kwargs: Any) -> None:
        # Changing the options reloads the entry, which starts the tunnel.
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, CONF_ENABLED: True}
        )
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, CONF_ENABLED: False}
        )
        self.async_write_ha_state()
