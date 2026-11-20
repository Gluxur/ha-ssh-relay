"""Constants for the SSH Relay integration."""

from __future__ import annotations

import re

DOMAIN = "ssh_relay"
PLATFORMS = ["sensor", "switch"]

CONF_RELAY_HOST = "relay_host"
CONF_SSH_PORT = "ssh_port"
CONF_TENANT = "tenant"
CONF_REMOTE_PORT = "remote_port"
CONF_LOCAL_HOST = "local_host"
CONF_LOCAL_PORT = "local_port"
CONF_KEEPALIVE = "keepalive"
CONF_PRIVATE_KEY = "private_key"
CONF_PUBLIC_KEY = "public_key"
CONF_HOST_KEY = "host_key"
CONF_ENABLED = "enabled"

DEFAULT_SSH_PORT = 22
DEFAULT_LOCAL_HOST = "homeassistant"
DEFAULT_LOCAL_PORT = 8123
DEFAULT_KEEPALIVE = 30

REMOTE_PORT_MIN = 18000
REMOTE_PORT_MAX = 18999
KEEPALIVE_MIN = 5
KEEPALIVE_MAX = 300

TENANT_RE = re.compile(r"^[a-z0-9-]{1,24}$")

# Bound on the connection test of the config flow (seconds).
TEST_TIMEOUT = 15
# Bound on waiting for the tunnel task to finish when stopping (seconds).
STOP_TIMEOUT = 5

ISSUE_HOST_KEY_CHANGED = "host_key_changed"
# Same issue, when the key the relay presented could not be read.
ISSUE_HOST_KEY_CHANGED_UNKNOWN = "host_key_changed_unknown"


def relay_username(tenant: str) -> str:
    """Unix user of a tenant on the relay."""
    return f"relay-{tenant}"


def issue_id(entry_id: str) -> str:
    """Repair issue id raised when the relay host key changed."""
    return f"{ISSUE_HOST_KEY_CHANGED}_{entry_id}"


def tenant_snippet(tenant: str, remote_port: int, public_key: str) -> str:
    """The tenant entry to add to the relay's tenants file."""
    return (
        f'"{tenant}": {{ "domain": "<your-domain>", "port": {remote_port}, '
        f'"public_key": "{public_key}" }}'
    )
