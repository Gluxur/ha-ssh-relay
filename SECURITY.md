# Security policy

SSH Relay exposes Home Assistant to the internet through a shared relay, and
handles SSH keys on both sides, so security reports are welcome.

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately through
[GitHub private vulnerability reporting](https://github.com/Gluxur/ha-ssh-relay/security/advisories/new).

Include what you can: affected version (integration and/or `relay-setup.sh`),
steps to reproduce, and impact. You should get a first answer within a week.

## Scope

- The integration (`custom_components/ssh_relay`).
- The relay configuration: `relay/relay-setup.sh` and the Terraform in
  `relay/oracle`, in particular anything that lets a tenant reach another
  tenant's port, a shell, or a public listener on the relay.

## Known limitations

- Terraform (`relay/oracle`) connects to the VM over SSH to run
  `relay-setup.sh` without pinning the VM host key. It authenticates with the
  admin key only, so a man in the middle gets no reusable credential, but
  could feed back a false result. The integration itself pins the relay host
  key.

## Supported versions

Only the latest release receives fixes.
