"""Load and resolve the frozen Zenoh v1 contract."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def contract_path() -> Path:
    env = os.environ.get("XGC2_B2_LINK_CONTRACT")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent.parent
    return here / "contract" / "zenoh_v1.yaml"


def load_contract(path: Optional[Path] = None) -> Dict[str, Any]:
    p = path or contract_path()
    if yaml is None:
        raise RuntimeError("PyYAML required to load contract (pip install pyyaml)")
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError(f"unsupported contract at {p}")
    return data


def key_prefix(robot_id: str, template: Optional[str] = None) -> str:
    tpl = template or "xgc2/{robot_id}"
    rid = robot_id.strip().strip("/")
    if not rid:
        raise ValueError("robot_id is required")
    return tpl.format(robot_id=rid)


def full_key(robot_id: str, relative_key: str, template: Optional[str] = None) -> str:
    rel = relative_key.lstrip("/")
    return f"{key_prefix(robot_id, template)}/{rel}"
