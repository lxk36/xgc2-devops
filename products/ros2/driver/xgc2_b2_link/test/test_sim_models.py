import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xgc2_b2_link.sim_models import (
    DRIVER_LEG_JOINTS,
    DRIVER_TO_URDF,
    arm_positions,
    odom_circle,
    remap_leg_to_urdf,
    walk_leg_positions,
)


def test_walk_has_12():
    p = walk_leg_positions(0.5)
    assert len(p) == 12
    assert len(DRIVER_LEG_JOINTS) == 12


def test_urdf_remap():
    names, pos = remap_leg_to_urdf(DRIVER_LEG_JOINTS, walk_leg_positions(0.0))
    assert all(n.startswith("b2_description_") for n in names)
    assert names[0] == DRIVER_TO_URDF[DRIVER_LEG_JOINTS[0]]
    assert len(pos) == 12


def test_odom_circle_moves():
    a = odom_circle(0.0)
    b = odom_circle(1.0)
    assert a["pose"]["position"]["x"] != b["pose"]["position"]["x"] or a["pose"]["position"]["y"] != b[
        "pose"
    ]["position"]["y"]


def test_arm_8():
    assert len(arm_positions(1.0)) == 8
