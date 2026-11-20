# Relay on Oracle Cloud (Always Free)

[`relay/oracle`](../relay/oracle) creates an Always Free VM on Oracle Cloud
Infrastructure (OCI) with a reserved public IP, opens ports 22, 443 and 8123,
adds a budget alert on any charge, and runs
[`relay-setup.sh`](../relay/relay-setup.sh) on the VM over SSH every time the
tenants list, the script or `remove_legacy` changes.

## Prerequisites

- An Oracle Cloud account (Always Free is enough; a payment card is asked at
  sign-up).
- [Terraform](https://developer.hashicorp.com/terraform/install) 1.4 or newer.
- The [OCI CLI](https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm),
  configured with `oci setup config` (creates `~/.oci/config` and an API key to
  upload in the console: Profile → My profile → API keys). Terraform uses it.
- An SSH key for the VM admin (user `ubuntu`):

  ```bash
  ssh-keygen -t ed25519 -C "ha-relay-admin" -f ~/.ssh/ha_relay_admin
  ```

- For each Home Assistant: a domain name, and the public key shown by the SSH
  Relay integration ([install-integration.md](install-integration.md), step 2;
  use **Save anyway** if the relay does not exist yet, with the domain as relay
  host). The tenants list needs at least one tenant, and a tenant is only added
  with its key.

## 1. Configure

```bash
cd relay/oracle
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` (never commit it):

| Variable | Value |
|---|---|
| `compartment_id` | Tenancy OCID (Profile → Tenancy → OCID). |
| `region` | Home region of your tenancy, e.g. `eu-paris-1`. |
| `availability_domain_index` | `0`; try `1` or `2` on "Out of host capacity". |
| `ssh_public_key` | Content of `~/.ssh/ha_relay_admin.pub`. |
| `admin_ssh_private_key_path` | `~/.ssh/ha_relay_admin`: Terraform connects with it to run `relay-setup.sh`. |
| `tenants` | One entry per Home Assistant (below). |
| `remove_legacy` | `false`. Only for a migration from the old `ha-tunnel` setup. |
| `alert_email` | Where the cost alerts go. |
| `vm_shape` | `VM.Standard.A1.Flex` (ARM, preferred) or `VM.Standard.E2.1.Micro` (x86, when ARM capacity is exhausted). |

```hcl
tenants = {
  alice = {
    domain     = "alice.duckdns.org"
    port       = 18123
    public_key = "ssh-ed25519 AAAA... ha-ssh-relay-alice"
  }
}
```

Rules (checked by Terraform and by the script): name `[a-z0-9-]{1,24}`, port an
integer in 18000-18999 and unique, domain a lowercase DNS name and unique,
public key a single-line `ssh-ed25519` key.

## 2. Create

```bash
terraform init
terraform plan
terraform apply
```

The first apply creates the network, the VM (cloud-init installs nginx,
`libnginx-mod-stream`, `iptables-persistent` and `jq`), the reserved IP and the
budget, then copies `relay-setup.sh` and `tenants.json` to
`/home/ubuntu/.ha-relay` (mode 700) on the VM, waits for cloud-init to finish
and runs `sudo bash /home/ubuntu/.ha-relay/relay-setup.sh /home/ubuntu/.ha-relay/tenants.json`. If SSH is not ready yet,
Terraform retries for a few minutes.

The end of the output shows the relay host key fingerprint:

```
relay-setup: host key fingerprint (pinned by the integration):
256 SHA256:... root@ha-relay (ED25519)
```

Outputs:

- `reserved_public_ip`: the relay IP;
- `ssh_admin_command`: `ssh -i <key> ubuntu@<ip>`;
- `tenants`: per tenant, the `domain` to point at the IP, the `ssh_user`
  (`relay-<name>`) and the `remote_port` to enter in the integration.

## 3. Point the domains at the relay

For each tenant, set the domain's A record to `reserved_public_ip`. With the
Duck DNS app in Home Assistant, set its `ipv4` option to the relay IP, or the
app keeps writing your home IP back.

## 4. Check

- In Home Assistant, the SSH Relay status becomes `connected` and its
  `host_key_fingerprint` attribute matches the fingerprint printed above.
- `https://<domain>` and `https://<domain>:8123` open Home Assistant from
  outside your network.

## Add, change or remove a tenant

Edit `tenants` and run `terraform apply`. A change of the list, of
`relay-setup.sh` or of `remove_legacy` replaces `terraform_data.relay_config`, which runs the script
again. Removing a tenant cuts its sessions and deletes its user. The VM, its IP
and its host key are never recreated by these changes: `metadata` (including
`user_data`) and `source_details` are ignored after creation.

To run the script again without a change (for example after editing files on
the VM by hand):

```bash
terraform apply -replace=terraform_data.relay_config
```

## Troubleshooting

- **Out of host capacity**: change `availability_domain_index`, or use
  `VM.Standard.E2.1.Micro`.
- **The script fails**: it restores the previous configuration, then exits
  with an error naming the cause (invalid tenant, `sshd -t`, `nginx -t`, a port
  already held by another program). Fix and apply again. Its backups sit next to
  each file (`/etc/ssh/sshd_config.bak.<timestamp>`, ...).
- **Status `waiting_for_relay` in Home Assistant**: the relay does not know the
  key yet (tenant missing or apply not run), or the key was regenerated.
- **Charges**: the budget alerts by email on any actual or forecast charge.
  Check that the tenancy is still Always Free.
