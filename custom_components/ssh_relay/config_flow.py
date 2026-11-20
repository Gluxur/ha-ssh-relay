"""Config and options flows for SSH Relay."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import asyncssh
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.translation import async_get_translations

from .const import (
    CONF_ENABLED,
    CONF_HOST_KEY,
    CONF_KEEPALIVE,
    CONF_LOCAL_HOST,
    CONF_LOCAL_PORT,
    CONF_PRIVATE_KEY,
    CONF_PUBLIC_KEY,
    CONF_RELAY_HOST,
    CONF_REMOTE_PORT,
    CONF_SSH_PORT,
    CONF_TENANT,
    DEFAULT_KEEPALIVE,
    DEFAULT_LOCAL_HOST,
    DEFAULT_LOCAL_PORT,
    DEFAULT_SSH_PORT,
    DOMAIN,
    KEEPALIVE_MAX,
    KEEPALIVE_MIN,
    REMOTE_PORT_MAX,
    REMOTE_PORT_MIN,
    TENANT_RE,
    TEST_TIMEOUT,
    issue_id,
    tenant_snippet,
)
from .coordinator import relay_config
from .tunnel import (
    CLOSE_TIMEOUT,
    ForwardRefused,
    HostKeyMismatch,
    generate_keypair,
    open_tunnel,
)

_LOGGER = logging.getLogger(__name__)

_TCP_PORT = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))
_REMOTE_PORT = vol.All(vol.Coerce(int), vol.Range(min=REMOTE_PORT_MIN, max=REMOTE_PORT_MAX))
_KEEPALIVE = vol.All(vol.Coerce(int), vol.Range(min=KEEPALIVE_MIN, max=KEEPALIVE_MAX))


def _settings_schema(defaults: dict[str, Any]) -> dict[vol.Marker, Any]:
    """Fields shared by the user step and the settings options step."""
    return {
        vol.Required(
            CONF_SSH_PORT, default=defaults.get(CONF_SSH_PORT, DEFAULT_SSH_PORT)
        ): _TCP_PORT,
        vol.Required(
            CONF_REMOTE_PORT,
            **({"default": defaults[CONF_REMOTE_PORT]} if CONF_REMOTE_PORT in defaults else {}),
        ): _REMOTE_PORT,
        vol.Required(
            CONF_LOCAL_HOST, default=defaults.get(CONF_LOCAL_HOST, DEFAULT_LOCAL_HOST)
        ): str,
        vol.Required(
            CONF_LOCAL_PORT, default=defaults.get(CONF_LOCAL_PORT, DEFAULT_LOCAL_PORT)
        ): _TCP_PORT,
        vol.Required(
            CONF_KEEPALIVE, default=defaults.get(CONF_KEEPALIVE, DEFAULT_KEEPALIVE)
        ): _KEEPALIVE,
    }


def _key_placeholders(data: dict[str, Any]) -> dict[str, str]:
    return {
        "public_key": data[CONF_PUBLIC_KEY],
        "tenant_snippet": tenant_snippet(
            data[CONF_TENANT], data[CONF_REMOTE_PORT], data[CONF_PUBLIC_KEY]
        ),
    }


async def _test_connection(data: dict[str, Any]) -> tuple[str | None, str | None]:
    """Open the tunnel once and close it. Returns (error key, relay host key)."""
    try:
        async with asyncio.timeout(TEST_TIMEOUT):
            conn, listener, host_key = await open_tunnel(relay_config(data))
    except asyncssh.PermissionDenied:
        return "invalid_auth", None
    except ForwardRefused:
        return "forward_refused", None
    except HostKeyMismatch:
        return "host_key_mismatch", None
    except (TimeoutError, OSError, asyncssh.Error) as err:
        _LOGGER.debug("Relay test connection failed: %s: %s", type(err).__name__, err)
        return "cannot_connect", None
    except Exception:  # noqa: BLE001 - report, never crash the flow
        _LOGGER.exception("Unexpected error testing the relay connection")
        return "cannot_connect", None
    listener.close()
    conn.close()
    try:
        async with asyncio.timeout(CLOSE_TIMEOUT):
            await conn.wait_closed()
    except TimeoutError:
        pass
    return None, host_key


async def _error_text(hass: HomeAssistant, error: str) -> str:
    """The translated config flow error message, for a menu description."""
    translations = await async_get_translations(
        hass, hass.config.language, "config", [DOMAIN]
    )
    return translations.get(f"component.{DOMAIN}.config.error.{error}", error)


class SshRelayConfigFlow(ConfigFlow, domain=DOMAIN):
    """Relay settings, then show the public key and test the tunnel."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SshRelayOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_RELAY_HOST].strip().lower()
            tenant = user_input[CONF_TENANT].strip()
            if not host:
                errors[CONF_RELAY_HOST] = "invalid_host"
            if not TENANT_RE.match(tenant):
                errors[CONF_TENANT] = "invalid_tenant"
            if not errors:
                await self.async_set_unique_id(f"{host}:{tenant}")
                self._abort_if_unique_id_configured()
                private, public = generate_keypair(f"ha-ssh-relay-{tenant}")
                self._data = {
                    **user_input,
                    CONF_RELAY_HOST: host,
                    CONF_TENANT: tenant,
                    CONF_LOCAL_HOST: user_input[CONF_LOCAL_HOST].strip(),
                    CONF_PRIVATE_KEY: private,
                    CONF_PUBLIC_KEY: public,
                }
                return await self.async_step_key()

        defaults = user_input or {}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_RELAY_HOST, default=defaults.get(CONF_RELAY_HOST, vol.UNDEFINED)
                ): str,
                vol.Required(
                    CONF_TENANT, default=defaults.get(CONF_TENANT, vol.UNDEFINED)
                ): str,
                **_settings_schema(defaults),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            error, host_key = await _test_connection(self._data)
            if error is None:
                return self._create({**self._data, CONF_HOST_KEY: host_key})
            return self.async_show_menu(
                step_id="key_failed",
                menu_options=["key", "save"],
                description_placeholders={
                    **_key_placeholders(self._data),
                    "reason": await _error_text(self.hass, error),
                },
            )
        return self.async_show_form(
            step_id="key",
            data_schema=vol.Schema({}),
            description_placeholders=_key_placeholders(self._data),
        )

    async def async_step_save(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Save without a pinned host key: the tunnel waits for the relay."""
        return self._create(dict(self._data))

    def _create(self, data: dict[str, Any]) -> ConfigFlowResult:
        if not data.get(CONF_HOST_KEY):
            data.pop(CONF_HOST_KEY, None)
        return self.async_create_entry(
            title=f"{data[CONF_TENANT]} @ {data[CONF_RELAY_HOST]}",
            data=data,
            options={CONF_ENABLED: True},
        )


class SshRelayOptionsFlow(OptionsFlow):
    """Change the settings, regenerate the client key or forget the host key."""

    def __init__(self) -> None:
        self._new_keys: tuple[str, str] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "regenerate_key", "forget_host_key"],
        )

    def _save(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Write entry.data and reload the entry; the options stay as they are."""
        entry = self.config_entry
        self.hass.config_entries.async_update_entry(entry, data=data)
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_create_entry(data=dict(entry.options))

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        data = dict(self.config_entry.data)
        if user_input is not None:
            return self._save(
                {
                    **data,
                    **user_input,
                    CONF_LOCAL_HOST: user_input[CONF_LOCAL_HOST].strip(),
                }
            )
        return self.async_show_form(
            step_id="settings", data_schema=vol.Schema(_settings_schema(data))
        )

    async def async_step_regenerate_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        data = dict(self.config_entry.data)
        if self._new_keys is None:
            self._new_keys = generate_keypair(f"ha-ssh-relay-{data[CONF_TENANT]}")
        private, public = self._new_keys
        if user_input is not None:
            # The relay host key stays pinned: only the client key changes.
            return self._save({**data, CONF_PRIVATE_KEY: private, CONF_PUBLIC_KEY: public})
        return self.async_show_form(
            step_id="regenerate_key",
            data_schema=vol.Schema({}),
            description_placeholders=_key_placeholders({**data, CONF_PUBLIC_KEY: public}),
        )

    async def async_step_forget_host_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            data = {k: v for k, v in self.config_entry.data.items() if k != CONF_HOST_KEY}
            ir.async_delete_issue(self.hass, DOMAIN, issue_id(self.config_entry.entry_id))
            return self._save(data)
        return self.async_show_form(step_id="forget_host_key", data_schema=vol.Schema({}))
