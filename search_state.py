"""Rotate role searches across cycles so each run stays fast."""

from __future__ import annotations

import json
from pathlib import Path

STATE_FILE = "search_state.json"


def load_state(data_dir: Path) -> dict:
    path = data_dir / STATE_FILE
    if not path.exists():
        return {"role_offset": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"role_offset": 0}


def save_state(data_dir: Path, state: dict) -> None:
    (data_dir / STATE_FILE).write_text(json.dumps(state, indent=2), encoding="utf-8")


def roles_for_cycle(config: dict, data_dir: Path) -> list[str]:
    all_roles = config.get("target_roles", [])
    if not all_roles:
        return []

    batch_size = config.get("settings", {}).get("roles_per_cycle", 0)
    if batch_size <= 0 or batch_size >= len(all_roles):
        return all_roles

    state = load_state(data_dir)
    offset = int(state.get("role_offset", 0)) % len(all_roles)
    batch = [all_roles[(offset + i) % len(all_roles)] for i in range(batch_size)]
    state["role_offset"] = (offset + batch_size) % len(all_roles)
    save_state(data_dir, state)
    print(
        f"Searching {len(batch)} roles this cycle (offset {offset} of {len(all_roles)})",
        flush=True,
    )
    return batch
