# Relay on any VPS (script only)

[`relay/relay-setup.sh`](../relay/relay-setup.sh) configures any Ubuntu or
Debian server as a relay, without Terraform. It is idempotent: run it again
after every change of the tenants list.

## Prerequisites

- Ubuntu 22.04+ or Debian 12+, with systemd and OpenSSH server, and a public
  IPv4.
- Root access with `sudo`, through an SSH **key** (the script disables
  password login for SSH, for every user).
- Ports **22**, **443** and **8123** open in the provider's firewall. The
  script opens them in iptables on the VM itself.
- Nothing else listening on 443 or 8123 (the script stops with
  `port 443 held by ...` otherwise). It disables nginx's default HTTP site:
  nothing listens on port 80.
- The public key of each Home Assistant, shown by the SSH Relay integration
  ([install-integration.md](install-integration.md), step 2).

The script installs what is missing: `nginx`, `libnginx-mod-stream`,
`iptables-persistent`, `jq`.

## 1. Write the tenants list

Start from [`relay/tenants.example.json`](../relay/tenants.example.json):

```json
{
  "tenants": {
    "alice": {
      "domain": "alice.duckdns.org",
      "port": 18123,
      "public_key": "ssh-ed25519 AAAA... ha-ssh-relay-alice"
    }
  }
}
```

Rules: name `[a-z0-9-]{1,24}` (Unix user `relay-<name>`), port an integer in
18000-18999 and unique, domain a lowercase DNS name and unique, public key a
single-line `ssh-ed25519` key. The list must not be empty.

## 2. Preview (optional)

`--render` validates the list and writes the files the script would install
into a directory, without root and without touching the system:

```bash
bash relay-setup.sh --render ./preview tenants.json
ls ./preview   # sshd_global.conf  sshd_match.conf  nginx.conf  authorized_keys/<name>
```

## 3. Apply

```bash
scp relay/relay-setup.sh tenants.json admin@relay.example.com:
ssh admin@relay.example.com sudo bash relay-setup.sh tenants.json
```

In order, the script:

1. installs the missing packages;
2. validates the list, and stops without changing anything if a tenant is
   invalid (`tenant <name>: <reason>`);
3. creates or updates each `relay-<name>` user (group `ha-relay`, shell
   `nologin`, locked password, `authorized_keys` with
   `restrict,port-forwarding,permitlisten="127.0.0.1:<port>"`), and removes
   the `ha-relay` users no longer in the list, after cutting their sessions;
4. writes `/etc/ssh/sshd_config.d/00-ha-relay.conf` (`PasswordAuthentication no`)
   and the `Match` blocks at the end of `/etc/ssh/sshd_config`, between
   `# BEGIN ha-relay` and `# END ha-relay`;
5. writes `/etc/nginx/stream.d/ha-relay.conf` (stream server on 443 and 8123,
   `ssl_preread`, domain → `127.0.0.1:<port>`, unknown names closed) and
   includes it from `nginx.conf`;
6. accepts 22, 443 and 8123 in iptables and saves the rules;
7. checks `sshd -t`, `nginx -t`, and with `sshd -T` that an admin login
   (checked as user `ubuntu`) gets no `ForceCommand` and no password login, and
   that each tenant can only listen on its port, then
   reloads sshd (open sessions stay up) and nginx;
8. checks that nginx really listens on 443 and 8123;
9. prints the host key fingerprint:

   ```
   relay-setup: host key fingerprint (pinned by the integration):
   256 SHA256:... root@relay (ED25519)
   ```

Every modified file is backed up next to itself (`<file>.bak.<timestamp>`).
From the first write until the listeners are checked, any failure (or `^C`, or
a dropped SSH session) restores those backups and the services.

Exit codes: `0` on success; `1` on an invalid list, a usage error or a failed
step (after rollback); `130` when interrupted.

## 4. Point the domains at the relay

Set each tenant domain's A record to the VPS IP. With the Duck DNS app in Home
Assistant, set its `ipv4` option to the relay IP, or it keeps writing your home
IP back. Then check, in Home Assistant, that the SSH Relay status is
`connected` and that its `host_key_fingerprint` attribute matches the
fingerprint printed by the script.

## Change the tenants

Edit `tenants.json`, copy it again and run the script again. Adding a tenant
creates its user; removing one cuts its sessions and deletes its user; a new
key replaces the old one.

## Legacy `ha-tunnel` user

If the server was used with the older single-tenant setup (user `ha-tunnel`
forwarding `0.0.0.0:8123`), the script keeps that user's sshd section as it is
and, while its tunnel holds 8123, only serves 443 and prints:

```
relay-setup: WARNING: 8123 held by ha-tunnel (legacy); run with --remove-legacy to take it over
```

It still exits `0`. See [migration-from-ssh-tunnel.md](migration-from-ssh-tunnel.md)
for the switch with `--remove-legacy`.
