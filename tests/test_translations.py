"""The French translation has exactly the keys of the English source."""

from __future__ import annotations

import json
from pathlib import Path

_DIR = Path(__file__).parents[1] / "custom_components" / "ssh_relay" / "translations"


def _flatten(node: object, prefix: str = "") -> set[str]:
    if not isinstance(node, dict):
        return {prefix}
    keys: set[str] = set()
    for key, value in node.items():
        keys |= _flatten(value, f"{prefix}.{key}" if prefix else key)
    return keys


def _keys(name: str) -> set[str]:
    return _flatten(json.loads((_DIR / name).read_text(encoding="utf-8")))


def test_fr_matches_en():
    en, fr = _keys("en.json"), _keys("fr.json")
    assert en - fr == set(), "missing in fr.json"
    assert fr - en == set(), "not in en.json"


def test_status_states_translated():
    states = {
        "connecting", "connected", "reconnecting", "disconnected",
        "error", "waiting_for_relay", "host_key_mismatch",
    }
    en = _keys("en.json")
    assert {f"entity.sensor.status.state.{s}" for s in states} <= en
