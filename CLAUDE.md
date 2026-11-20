# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Home Assistant custom integration (HACS, domain `ssh_relay`, HA 2025.1+) that keeps a key-only reverse SSH tunnel (`ssh -R 127.0.0.1:<port>`) open to a relay VM, plus the relay side:

- `relay/relay-setup.sh`: idempotent, root, configures any Ubuntu/Debian relay from a tenants JSON (users `relay-<name>`, sshd `Match` block between `# BEGIN/END ha-relay` markers, nginx stream + `ssl_preread` on 443/8123 routing by SNI, iptables). Rolls back on failure. `--render <dir>` renders without root; `--remove-legacy` takes port 8123 over from the old `ha-tunnel` user.
- `relay/oracle/`: OCI Always Free Terraform. `terraform_data.relay_config` copies the script and `tenants.json` and runs it over SSH when the tenants, the script or `remove_legacy` change.

User docs: `README.md`, `docs/install-*.md`, `docs/migration-from-ssh-tunnel.md`. `docs/superpowers/` and `.superpowers/` are gitignored working notes.

## Commands

```bash
pip install -r requirements_test.txt   # pytest, pytest-asyncio, asyncssh — no homeassistant
pytest                                 # all tests (asyncio_mode = auto, see pytest.ini)
shellcheck relay/relay-setup.sh && shellcheck -x tests/integration/sshd_check.sh
# real OpenSSH 8.9 / nginx check (root, throwaway Ubuntu 22.04 container; see the script header)
terraform -chdir=relay/oracle fmt -check && terraform -chdir=relay/oracle init -backend=false && terraform -chdir=relay/oracle validate
```

CI (`.github/workflows/validate.yml`) runs HACS validation, hassfest, pytest, shellcheck, `tests/integration/sshd_check.sh` (in an `ubuntu:22.04` container: the written sshd config is checked by the real `sshd -t`/`sshd -T`, the nginx config by `nginx -t`) and terraform fmt/validate. The HACS job is gated with `if: ${{ (github.event.repository && !github.event.repository.private) || vars.REPO_PUBLIC == 'true' }}`: HACS cannot read a private repository, so it is skipped while the repo is private. Push/PR/dispatch payloads carry `repository.private`; the `schedule` payload has no `repository`, so the weekly run only validates HACS once the repository variable `REPO_PUBLIC` is set to `true` (set it when making the repo public: `gh variable set REPO_PUBLIC --body true`).

## Rules

- `tunnel.py` must stay free of Home Assistant imports (and relative imports): tests load it by file path, since Home Assistant is not installed in the test environment.
- Tenant validation rules (name `[a-z0-9-]{1,24}`, port 18000-18999 unique, lowercase DNS domain unique, single-line `ssh-ed25519` key) exist in three places that must stay in sync: `validate_tenants` in `relay-setup.sh`, the `tenants` validations in `relay/oracle/variables.tf`, and `const.py` (`TENANT_RE`, port range).
- `relay/oracle` keeps the resource names of the Terraform it was migrated from, so existing state transfers without recreation. Never rename them, and keep `lifecycle.ignore_changes = [metadata, source_details]` on the instance.
- No personal data (names, domains, IPs, emails) in committed files: examples use `alice`/`bob`, `*.duckdns.org`, `you@example.com`.
- Docs must match the code: config flow fields/steps (`config_flow.py`, `translations/en.json`) and the script's CLI, messages and exit codes.
- Release: `gh release create vX.Y.Z --generate-notes` with `ssh_relay.zip` attached (the `ssh_relay/` folder, ready for `custom_components/`); bump `manifest.json` `version` first.
