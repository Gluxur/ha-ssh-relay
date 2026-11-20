# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Home Assistant custom integration (HACS, domain `ssh_relay`) plus reverse-SSH relay infrastructure (`relay/`: `relay-setup.sh`, Terraform in `relay/oracle`). Requires HA 2025.1+. This file is a minimal stub, to be completed later.

## Commands

```bash
pip install -r requirements_test.txt   # pytest, pytest-asyncio, asyncssh — no homeassistant
pytest                                 # all tests (asyncio_mode = auto, see pytest.ini)
```

CI (`.github/workflows/validate.yml`) runs HACS validation, hassfest, pytest, shellcheck and terraform fmt/validate.

## Rules

- `tunnel.py` must stay free of Home Assistant imports (and relative imports): tests load it by file path, since Home Assistant is not installed in the test environment.
