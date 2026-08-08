import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xgc2_b2_link.codec import (
    high_level_cmd,
    pack_cdr_ros2,
    pack_json,
    power_summary_from_low_state_fields,
    unpack_cdr_ros2,
    unpack_json,
)
from xgc2_b2_link.contract import full_key, load_contract
from xgc2_b2_link.rate import RateGate


def test_contract_loads():
    c = load_contract()
    assert c["version"] == 1
    assert "up/odom" == c["channels"]["up"]["odom"]["key"]


def test_keys():
    assert full_key("b2-01", "up/odom") == "xgc2/b2-01/up/odom"


def test_cdr_envelope_roundtrip():
    blob = pack_cdr_ros2("nav_msgs/msg/Odometry", b"\x01\x02")
    t, body = unpack_cdr_ros2(blob)
    assert t == "nav_msgs/msg/Odometry"
    assert body == b"\x01\x02"


def test_power_summary_json():
    obj = power_summary_from_low_state_fields(soc=42, power_v=47.5, power_a=-1.0, t_ms=1)
    again = unpack_json(pack_json(obj))
    assert again["soc"] == 42
    assert again["v"] == 1


def test_cmd_schema():
    c = high_level_cmd("echo_value", "hi", ts_ms=9)
    assert c["type"] == "echo_value"
    assert unpack_json(pack_json(c))["value"] == "hi"


def test_rate_gate():
    g = RateGate(10.0)
    assert g.allow(0.0) is True
    assert g.allow(0.01) is False
    assert g.allow(0.11) is True
