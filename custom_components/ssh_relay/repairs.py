"""Repair flow: accept a changed relay host key."""

from __future__ import annotations

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_HOST_KEY


class HostKeyChangedRepairFlow(ConfirmRepairFlow):
    """Pin the host key the relay presented (the one shown in the issue).

    When that key could not be read, forget the pinned key instead: the next
    connection pins whatever the relay presents.
    """

    def __init__(self, entry_id: str | None, host_key: str | None = None) -> None:
        super().__init__()
        self._entry_id = entry_id
        self._host_key = host_key

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        if user_input is not None and self._entry_id:
            entry = self.hass.config_entries.async_get_entry(self._entry_id)
            if entry is not None:
                if self._host_key:
                    data = {**entry.data, CONF_HOST_KEY: self._host_key}
                else:
                    data = {k: v for k, v in entry.data.items() if k != CONF_HOST_KEY}
                self.hass.config_entries.async_update_entry(entry, data=data)
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return await super().async_step_confirm(user_input)


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    entry_id = data.get("entry_id") if data else None
    host_key = data.get("host_key") if data else None
    return HostKeyChangedRepairFlow(
        entry_id if isinstance(entry_id, str) else None,
        host_key if isinstance(host_key, str) else None,
    )
