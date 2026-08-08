"""Payload codecs shared by G3 forwarder and G4 ground adapter."""

from __future__ import annotations

import json
import struct
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

MAGIC = b"XGC2"
ENVELOPE_VERSION = 1


def now_ms() -> int:
    return int(time.time() * 1000)


def pack_cdr_ros2(type_name: str, cdr_bytes: bytes) -> bytes:
    """Wire: magic(4) | version(u8) | type_len(u16be) | type_utf8 | cdr."""
    t = type_name.encode("utf-8")
    if len(t) > 65535:
        raise ValueError("type name too long")
    return MAGIC + bytes([ENVELOPE_VERSION]) + struct.pack(">H", len(t)) + t + cdr_bytes


def unpack_cdr_ros2(blob: bytes) -> Tuple[str, bytes]:
    if len(blob) < 7 or blob[:4] != MAGIC:
        raise ValueError("bad CDR envelope magic")
    ver = blob[4]
    if ver != ENVELOPE_VERSION:
        raise ValueError(f"unsupported envelope version {ver}")
    (tlen,) = struct.unpack(">H", blob[5:7])
    end = 7 + tlen
    if len(blob) < end:
        raise ValueError("truncated type name")
    type_name = blob[7:end].decode("utf-8")
    return type_name, blob[end:]


def pack_json(obj: Dict[str, Any]) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def unpack_json(blob: bytes) -> Dict[str, Any]:
    data = json.loads(blob.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("json payload must be object")
    return data


def power_summary_from_low_state_fields(
    *,
    soc: Any = None,
    power_v: Any = None,
    power_a: Any = None,
    temperature_ntc1: Any = None,
    temperature_ntc2: Any = None,
    t_ms: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "v": 1,
        "t_ms": t_ms if t_ms is not None else now_ms(),
        "soc": soc,
        "power_v": power_v,
        "power_a": power_a,
        "temperature_ntc1": temperature_ntc1,
        "temperature_ntc2": temperature_ntc2,
    }


def high_level_cmd(
    cmd_type: str,
    value: str = "",
    *,
    source: str = "xgc2-gcs",
    ts_ms: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "v": 1,
        "type": cmd_type,
        "value": value,
        "source": source,
        "ts_ms": ts_ms if ts_ms is not None else now_ms(),
    }


def arm_status_json(
    joint_pos,
    *,
    joint_vel=None,
    joint_cur=None,
    end_pos=None,
    t_ms: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "v": 1,
        "t_ms": t_ms if t_ms is not None else now_ms(),
        "joint_pos": list(joint_pos) if joint_pos is not None else [],
        "joint_vel": list(joint_vel) if joint_vel is not None else [],
        "joint_cur": list(joint_cur) if joint_cur is not None else [],
        "end_pos": list(end_pos) if end_pos is not None else [],
    }
