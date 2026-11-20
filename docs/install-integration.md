# Install and configure the integration

The integration opens the reverse tunnel from Home Assistant to the relay. It
needs a relay that knows its tenant (see
[install-relay-oracle.md](install-relay-oracle.md) or
[install-relay-vps.md](install-relay-vps.md)), but it can be set up first: it
generates the key the relay needs.

## Before you start

- Home Assistant 2025.1 or newer.
- Home Assistant serves **HTTPS** for the domain you will use (for example the
  Duck DNS app with Let's Encrypt, which uses a DNS challenge and needs no open
  port). The relay forwards the TLS stream as is: the certificate presented to
  your phone is the one of Home Assistant.
- From the relay admin, a **tenant name** (`[a-z0-9-]`, up to 24 characters)
  and a **port** in 18000-18999 reserved for you.

## 1. Install

### HACS (recommended)

1. In HACS, search for **SSH Relay** (it is in the default list).
2. Open it → **Download**.
3. Restart Home Assistant.

HACS then offers new releases as updates.

If you installed by hand before, delete `/config/custom_components/ssh_relay`
first; your config entries are kept.

### Manual copy

1. Download `ssh_relay.zip` from the
   [latest release](https://github.com/Gluxur/ha-ssh-relay/releases/latest).
2. Extract it into `/config/custom_components/` (with the Samba, SSH or File
   editor app). The result must be `/config/custom_components/ssh_relay/manifest.json`.
3. Restart Home Assistant.

To update, replace the `ssh_relay` folder with the one of the new release and
restart.

## 2. Add the integration

Settings → Devices & services → **Add integration** → **SSH Relay**.

### Step 1: Connect to an SSH relay

| Field | Value |
|---|---|
| Relay host | IP address or hostname of the relay. If you do not know the IP yet, the domain you will point at the relay works too. |
| SSH port | 22, unless the relay admin says otherwise. |
| Tenant name | Your tenant name. The relay user is `relay-<tenant name>`. |
| Port on the relay | Your port, 18000-18999. Must be the one of your tenant on the relay. |
| Local host | Where the relayed traffic goes. `homeassistant` on Home Assistant OS; `127.0.0.1` for Container or Core installs. |
| Local port | The port Home Assistant serves HTTPS on, usually 8123. |
| Keepalive interval (seconds) | 5-300, default 30. |

One relay host + tenant name pair can only be configured once. A Home
Assistant can have several entries (for example two relays).

### Step 2: Add the key to the relay

The integration generates an ed25519 key pair and shows:

- the **tenant entry** for the relay's list, for example
  `"alice": { "domain": "<your-domain>", "port": 18123, "public_key": "ssh-ed25519 AAAA... ha-ssh-relay-alice" }`;
- the **public key** alone.

Replace `<your-domain>` with the name you reach Home Assistant at and send the
entry to the relay admin (or add it yourself, then run `relay-setup.sh` or
`terraform apply`). The public key is not secret; the private key never leaves
Home Assistant.

The key pair lives only in this setup dialog until the entry is created: if the
dialog is closed or Home Assistant restarts ("Invalid flow specified"), starting
over generates a **new** key, which must replace the old one on the relay.
Either keep the dialog open until the relay admin has applied the key, or
choose **Save anyway** at step 3 right away.

### Step 3: Test

**Submit** opens the tunnel once and closes it. On success, the relay host key
is pinned and the entry is created.

If the test fails, the reason is shown with two choices:

- **Test again**, once the relay is updated;
- **Save anyway**: the entry is created without a pinned host key. The status
  stays `waiting_for_relay` and the tunnel retries until the relay knows the
  key, then pins the host key on that first connection.

Reasons: the relay rejected the key (not on the relay yet), the relay could not
be reached (host, SSH port, network), the relay refused the port forward (not
the port of this tenant, or used by another one), or the relay presented an
unexpected host key.

## 3. Check

The device **SSH Relay** has:

- **Status**: `connecting`, `connected`, `reconnecting`, `disconnected`,
  `error`, `waiting_for_relay` or `host_key_mismatch`. Attributes: relay host,
  tenant, remote port, `host_key_fingerprint`, last error, last connection time.
- **Uptime** and **Reconnect count**.
- **Enabled**: turn it off to close the tunnel without removing the entry.

Compare `host_key_fingerprint` (`SHA256:...`) with the fingerprint printed at
the end of `relay-setup.sh` on the relay. Then open `https://<your-domain>`
(port 443) or `https://<your-domain>:8123` from outside your network, and set
that URL as the external URL in the Home Assistant app.

## Options

**Configure** on the integration:

- **Ports, local host and keepalive**: SSH port, port on the relay, local host,
  local port, keepalive. A new port on the relay must also be changed in the
  relay's tenants list.
- **Generate a new client key**: shows a new public key and tenant entry.
  Replace the key on the relay and apply; until then the relay rejects the
  tunnel. The relay host key stays pinned.
- **Forget the relay host key**: the next connection pins whatever key the
  relay presents. Only after a relay reinstall.

## Relay host key changed

If the relay presents another host key than the pinned one, the tunnel stays
closed, the status becomes `host_key_mismatch` and a repair issue shows the
expected and actual fingerprints. Accept it only if the relay was reinstalled
(compare with the fingerprint printed by `relay-setup.sh`); otherwise someone
may be impersonating the relay.
