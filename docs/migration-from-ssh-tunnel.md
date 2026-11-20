# Migration from a single-tenant `ssh_tunnel` relay

This guide moves an existing single-tenant relay to SSH Relay, with a cut of a
few minutes and **the same URL**. It applies to a relay where:

- a user **`ha-tunnel`** opens `ssh -R 8123` (bound on `0.0.0.0:8123`,
  `GatewayPorts yes`), from a custom `ssh_tunnel` integration in Home
  Assistant;
- the VM was created by the single-tenant Oracle Terraform that
  [`relay/oracle`](../relay/oracle) derives from (same resource names), so its
  state can be reused. Without Terraform, follow the same steps with
  `relay-setup.sh` alone ([install-relay-vps.md](install-relay-vps.md)): step 3
  is `sudo bash relay-setup.sh tenants.json`, step 4 is
  `sudo bash relay-setup.sh --remove-legacy tenants.json`.

The old and new tunnels do not conflict: the new one listens on
`127.0.0.1:<port>` and only takes 8123 over at the switch. Until the last step,
`ha-tunnel` and its key stay in place, so you can go back.

Below, `<domain>` is your Home Assistant domain, `<tenant>` your tenant name and
18123 its port.

## 0. Survey the relay

Before changing anything, look at what is really on the VM:

```bash
sudo cat /etc/ssh/sshd_config
sudo sshd -T -C user=ha-tunnel,host=x,addr=1.2.3.4 | grep -Ei 'permitlisten|gatewayports|allowtcpforwarding|passwordauthentication|forcecommand'
sudo cat ~ha-tunnel/.ssh/authorized_keys
```

Also check how the current `ssh_tunnel` integration really authenticates
(password or key): the relay must keep accepting it until the switch.

`relay-setup.sh` recognises two forms of the old sshd configuration, and
removes them before writing its own block:

- a block between `# BEGIN ha-relay` and `# END ha-relay`;
- an unmarked block from a `GatewayPorts yes` line to the end of the file.

The `Match User ha-tunnel` section found there is carried over **verbatim**
until `--remove-legacy`. If the file looks different, the script refuses to
touch it (`refusing to touch /etc/ssh/sshd_config: the ha-tunnel config would be
lost`): fix it by hand into one of the two forms first. To see what would be
written, without changing anything:

```bash
bash relay-setup.sh --render ./preview tenants.json
```

## 1. Install SSH Relay next to `ssh_tunnel`

Install the integration ([install-integration.md](install-integration.md)) and
add an entry: relay host = the relay IP (or `<domain>`), tenant `<tenant>`,
port on the relay 18123. At the key step, copy the public key, then submit and
choose **Save anyway** (the relay does not know the key yet). Turn its
**Enabled** switch off: `ssh_tunnel` keeps serving Home Assistant.

## 2. Move the Terraform state

Copy the existing `terraform.tfstate` into `relay/oracle/`. Write
`terraform.tfvars` from the old one and
[`terraform.tfvars.example`](../relay/oracle/terraform.tfvars.example): drop
`ha_tunnel_public_key`, add `admin_ssh_private_key_path` and `tenants`. Then:

```bash
cd relay/oracle
terraform init
terraform plan
```

The plan must **not** replace `oci_core_instance.relay` or
`oci_core_public_ip.relay`. Expected: an in-place update of the security list
(new 443 rule), in-place text updates of the budget and its alerts, and the
creation of `terraform_data.relay_config`. **If the VM or the IP would be
recreated, stop**: the IP and the host key would change.

## 3. Add the tenant, without the switch

```hcl
tenants = {
  "<tenant>" = {
    domain     = "<domain>"
    port       = 18123
    public_key = "ssh-ed25519 AAAA... ha-ssh-relay-<tenant>"
  }
}
remove_legacy = false
```

```bash
terraform apply
```

The script installs nginx, creates `relay-<tenant>` and serves 443. Port 8123
is still held by the old tunnel, so it prints, and still succeeds:

```
relay-setup: WARNING: 8123 held by ha-tunnel (legacy); run with --remove-legacy to take it over
```

No cut. Optionally, turn SSH Relay on now and check `https://<domain>` (port
443) from outside: both tunnels can run side by side.

## 4. Switch (a few minutes)

```hcl
remove_legacy = true
```

```bash
terraform apply
```

The script locks the `ha-tunnel` sshd section (no forwarding, `nologin`), cuts
its session, and nginx takes 8123. `ha-tunnel` and its `authorized_keys` are
kept. Then, in Home Assistant:

1. disable `ssh_tunnel`;
2. turn SSH Relay's **Enabled** switch on;
3. check that its status is `connected` and that `host_key_fingerprint` matches
   the fingerprint printed by the script;
4. open `https://<domain>:8123` from outside, and the mobile app.

## 5. Clean up (after a few days without problems)

Remove the old user on the relay:

```bash
sudo pkill -u ha-tunnel
sudo userdel -r ha-tunnel
```

Then set `remove_legacy = false` and `terraform apply` (the change of
`remove_legacy` reruns the script): without the user, the locked
`Match User ha-tunnel` section is dropped. Keep `remove_legacy = true` until
this point: flipping it earlier, while `ha-tunnel` still exists, only reruns the
script in keep mode, which carries the locked section over as it is (still
locked), so it gains nothing; after `userdel`, the flip is what triggers the
run that removes the section. Finally, remove `ssh_tunnel` from
Home Assistant.

## Going back (before step 5)

`ha-tunnel` and its key are still there. Each run of the script backs up the
files it changes next to them, with the same timestamp. Restore the backups of
the step 4 run (`--remove-legacy`), which hold the pre-switch configuration:

```bash
ls /etc/ssh/sshd_config.bak.* /etc/nginx/stream.d/ha-relay.conf.bak.*
sudo cp -a /etc/ssh/sshd_config.bak.<timestamp> /etc/ssh/sshd_config
sudo cp -a /etc/nginx/stream.d/ha-relay.conf.bak.<timestamp> /etc/nginx/stream.d/ha-relay.conf
sudo sshd -t && sudo systemctl reload ssh
sudo nginx -t && sudo systemctl reload nginx
```

nginx then releases 8123. Turn SSH Relay off, re-enable `ssh_tunnel`, and set
`remove_legacy = false` in `terraform.tfvars` so that a later apply keeps the
legacy section.
