"""SSH Relay: reverse SSH tunnel from Home Assistant to a relay VM."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import CONF_ENABLED, DOMAIN, PLATFORMS, issue_id
from .coordinator import RelayCoordinator

type SshRelayConfigEntry = ConfigEntry[RelayCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: SshRelayConfigEntry) -> bool:
    coordinator = RelayCoordinator(hass, entry)
    entry.runtime_data = coordinator

    if entry.options.get(CONF_ENABLED, True):
        coordinator.start()

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        # No unload will follow a failed setup: do not leave the tunnel running.
        await coordinator.stop()
        raise
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SshRelayConfigEntry) -> bool:
    await entry.runtime_data.stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry.entry_id))


async def _async_update_listener(hass: HomeAssistant, entry: SshRelayConfigEntry) -> None:
    # Pinning the host key only changes entry.data: no reload for that.
    if dict(entry.options) != entry.runtime_data.options:
        await hass.config_entries.async_reload(entry.entry_id)
