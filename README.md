# SSH Relay for Home Assistant

[![HACS](https://img.shields.io/badge/HACS-custom%20repository-41BDF5.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Gluxur&repository=ha-ssh-relay&category=integration)
[![Validate](https://github.com/Gluxur/ha-ssh-relay/actions/workflows/validate.yml/badge.svg)](https://github.com/Gluxur/ha-ssh-relay/actions/workflows/validate.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Reach Home Assistant from the internet **without opening a port at home**: Home Assistant opens a **key-only reverse SSH tunnel** to a small relay VM, and the relay routes each HTTPS connection to the right Home Assistant by its domain name. One free relay can serve several homes.

## Why

Behind CGNAT, a 4G/5G box or a router you do not control, no port forwarding is possible. A reverse SSH tunnel to a VM with a public IP works anywhere outbound SSH works, but a plain `ssh -R 8123` gives one port per VM, a public listener per tunnel and, often, a password.

SSH Relay keeps the tunnel and fixes the rest:

```
phone/app ──TLS (SNI)──▶ relay :443 and :8123     nginx stream + ssl_preread (no decryption)
                           ├─ alice.duckdns.org → 127.0.0.1:18123 ─┐
                           └─ bob.duckdns.org   → 127.0.0.1:18124 ─┤ reverse SSH tunnels,
                                                                   │ opened from each home
          Home Assistant (alice) ◀── relay-alice  (ssh -R 127.0.0.1:18123) ┘
          Home Assistant (bob)   ◀── relay-bob    (ssh -R 127.0.0.1:18124)
```

- Each Home Assistant is a **tenant**: its own Unix user `relay-<name>`, its own key, and the right to listen on **one loopback port only**.
- nginx reads the server name from the TLS handshake and forwards the encrypted stream to the tenant's port. TLS ends in Home Assistant, with its own certificate: the relay never sees your traffic in clear.
- The integration generates the SSH key itself (ed25519, no password anywhere), pins the relay host key on the first connection, and reconnects with backoff.

## Requirements

- Home Assistant 2025.1 or newer (any installation type).
- Home Assistant serving **HTTPS** itself (`http:` `ssl_certificate` / `ssl_key`, for example with the Duck DNS app and Let's Encrypt). The relay routes on the TLS server name: plain HTTP cannot be routed.
- A domain name per Home Assistant, pointing at the relay's public IP (a DuckDNS subdomain is enough).
- A relay: a Linux VM with a public IPv4 and ports 22, 443 and 8123 reachable.
  - **Oracle Cloud Always Free**, created with the Terraform in [`relay/oracle`](relay/oracle): [docs/install-relay-oracle.md](docs/install-relay-oracle.md).
  - Or **any Ubuntu/Debian VPS**, configured with the script alone: [docs/install-relay-vps.md](docs/install-relay-vps.md).

## Install

While the repository is private, download `ssh_relay.zip` from the [latest release](https://github.com/Gluxur/ha-ssh-relay/releases/latest), extract it into `/config/custom_components/` (you get `/config/custom_components/ssh_relay/`) and restart Home Assistant.

Once the repository is public, it can be added to HACS as a custom repository (category *Integration*):

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Gluxur&repository=ha-ssh-relay&category=integration)

Details: [docs/install-integration.md](docs/install-integration.md).

## Configure

[![Open your Home Assistant instance and start setting up SSH Relay.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ssh_relay)

Or: Settings → Devices & services → **Add integration** → **SSH Relay**:

1. Enter the relay host, the tenant name and the port reserved for it on the relay (18000-18999). The defaults for SSH port (22), local host (`homeassistant`), local port (8123) and keepalive (30 s) fit Home Assistant OS.
2. The next step shows the generated public key and a tenant entry ready to paste into the relay's tenants list. Add it on the relay (Terraform `tenants` or `tenants.json`), then apply.
3. Submit to test the tunnel. If the relay does not know the key yet, choose **Save anyway**: the tunnel waits for the relay and connects as soon as the key is there.

Options (**Configure** on the integration): ports, local host and keepalive; generate a new client key; forget the relay host key.

## How it works

On the relay, [`relay/relay-setup.sh`](relay/relay-setup.sh) turns a tenants list into the whole configuration, idempotently:

```json
{"tenants": {"alice": {"domain": "alice.duckdns.org", "port": 18123, "public_key": "ssh-ed25519 AAAA..."}}}
```

- one user `relay-<name>` per tenant (group `ha-relay`, no shell, locked password), whose `authorized_keys` only allows `permitlisten="127.0.0.1:<port>"`;
- an sshd block that applies to the `ha-relay` group: public key only, remote forwarding only, no TTY, no agent/X11/stream-local forwarding, `GatewayPorts no`;
- an nginx stream server on 443 and 8123 with `ssl_preread`, mapping each domain to its tenant's port; unknown names are closed;
- firewall rules for 22, 443 and 8123;
- checks (`sshd -t`, `nginx -t`, `sshd -T` per tenant, real listeners) with rollback to the previous files on any failure, then prints the relay host key fingerprint.

In Home Assistant, each config entry keeps one tunnel open and exposes a **Status** sensor (`connecting`, `connected`, `reconnecting`, `disconnected`, `error`, `waiting_for_relay`, `host_key_mismatch`), an **Uptime** sensor, a **Reconnect count** sensor and an **Enabled** switch. The status sensor's `host_key_fingerprint` attribute shows the pinned relay key, to compare with the script output.

Coming from a single-tenant `ssh -R` setup (user `ha-tunnel`, custom `ssh_tunnel` integration)? See [docs/migration-from-ssh-tunnel.md](docs/migration-from-ssh-tunnel.md).

## Security

- **No passwords.** The integration authenticates with an ed25519 key it generates; the relay accepts public keys only for tenant users and disables password login globally.
- **One port per tenant.** A tenant key can only open a remote listener on `127.0.0.1:<its port>`: no shell, no local forwarding, no other port, and nothing bound on a public address. A tenant cannot take over another tenant's port or domain.
- **No decryption on the relay.** nginx forwards the TLS stream by its server name; certificates and keys stay in each Home Assistant.
- **Pinned relay host key.** The first successful connection pins the relay ed25519 host key. A different key closes the tunnel and raises a repair issue; it is never accepted automatically.
- **Secrets.** The private key is stored in the Home Assistant config entry (`/config/.storage`, so also in backups) and is never logged.
- Home Assistant sees relayed connections as coming from a local address, not from the real client. Keep strong passwords and multi-factor authentication on your Home Assistant users.
- Anyone with admin access to the relay can stop or redirect tunnels (not read the TLS traffic). Only share a relay with people who trust its admin.
- Report vulnerabilities privately, see [SECURITY.md](SECURITY.md).

## Limitations

- HTTPS only, with SNI: clients that do not send a server name are closed.
- The relay needs ports 443 and 8123 for itself: nothing else may listen on them.
- IPv4 relay; no automatic DNS update (point each domain at the relay IP once).

## Uninstall

Remove the integration in Settings → Devices & services, then delete `custom_components/ssh_relay` (or remove it from HACS). On the relay, remove the tenant from the list and apply: its user and sessions are removed.

## Disclaimer

This is a community project. It is not affiliated with or endorsed by Nabu Casa / Home Assistant or Oracle.

## Development

```bash
pip install -r requirements_test.txt
pytest
```
