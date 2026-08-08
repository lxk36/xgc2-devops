"""Simulated B2 + R5a kinematics for closed-loop visualization tests.

Joint names follow:
- Wire/driver style (Unitree): FR_hip_joint, … (12 DOF)
- URDF b2arx_visual: b2_description_*_joint + R5a_joint*
Ground recovery remaps driver → URDF for robot_state_publisher + Lichtblick.
"""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple

# Field driver joint order (b2_ros2_driver state_converter)
DRIVER_LEG_JOINTS: List[str] = [
    "FR_hip_joint",
    "FR_thigh_joint",
    "FR_calf_joint",
    "FL_hip_joint",
    "FL_thigh_joint",
    "FL_calf_joint",
    "RR_hip_joint",
    "RR_thigh_joint",
    "RR_calf_joint",
    "RL_hip_joint",
    "RL_thigh_joint",
    "RL_calf_joint",
]

# Map driver short names → b2arx_visual.urdf revolute names
DRIVER_TO_URDF: Dict[str, str] = {
    "FR_hip_joint": "b2_description_FR_hip_joint",
    "FR_thigh_joint": "b2_description_FR_thigh_joint",
    "FR_calf_joint": "b2_description_FR_calf_joint",
    "FL_hip_joint": "b2_description_FL_hip_joint",
    "FL_thigh_joint": "b2_description_FL_thigh_joint",
    "FL_calf_joint": "b2_description_FL_calf_joint",
    "RR_hip_joint": "b2_description_RR_hip_joint",
    "RR_thigh_joint": "b2_description_RR_thigh_joint",
    "RR_calf_joint": "b2_description_RR_calf_joint",
    "RL_hip_joint": "b2_description_RL_hip_joint",
    "RL_thigh_joint": "b2_description_RL_thigh_joint",
    "RL_calf_joint": "b2_description_RL_calf_joint",
}

# Arm status JSON uses R5a joint names matching URDF (revolute 1–6 + prismatic 7–8)
ARM_URDF_JOINTS: List[str] = [
    "R5a_joint1",
    "R5a_joint2",
    "R5a_joint3",
    "R5a_joint4",
    "R5a_joint5",
    "R5a_joint6",
    "R5a_joint7",
    "R5a_joint8",
]

ODOM_FRAME = "odom"
BASE_FRAME = "b2_description"  # URDF root link

# Nominal body height so foot links sit near z=0 for b2arx_visual.urdf.
# FK (walk stance): foot ≈ -0.64 m in base frame → base z ≈ 0.64.
# Real B2: prefer state-estimator / odom.z when available; LowState does not
# always expose a calibrated world height without extrinsics.
STANDING_BASE_Z = 0.64


def yaw_to_quat(yaw: float) -> Tuple[float, float, float, float]:
    """Return (x, y, z, w)."""
    half = 0.5 * yaw
    return (0.0, 0.0, math.sin(half), math.cos(half))


def walk_leg_positions(t: float, phase: float = 0.0) -> List[float]:
    """12-DOF simple trot-like positions (rad)."""
    w = 2.0 * math.pi * 1.2  # ~1.2 Hz gait
    a_hip = 0.12
    a_thigh = 0.28
    a_calf = 0.35
    # pairs: FR, FL, RR, RL — diagonal trot
    phases = [0.0 + phase, math.pi + phase, math.pi + phase, 0.0 + phase]
    out: List[float] = []
    for p in phases:
        s = math.sin(w * t + p)
        # Nominal stance (thigh/calf) keeps feet near ground when base_z=STANDING_BASE_Z
        out.extend([a_hip * s * 0.3, 0.55 + a_thigh * s * 0.5, -1.05 - a_calf * abs(s) * 0.25])
    return out


def arm_positions(t: float) -> List[float]:
    """8 arm DOF: 6 revolute + 2 prismatic gripper (m)."""
    return [
        0.3 * math.sin(0.4 * t),
        0.8 + 0.2 * math.sin(0.5 * t),
        1.0 + 0.15 * math.cos(0.45 * t),
        0.2 * math.sin(0.6 * t),
        0.4 * math.sin(0.35 * t),
        0.3 * math.cos(0.55 * t),
        0.02 + 0.01 * math.sin(1.0 * t),  # prismatic
        0.02 + 0.01 * math.sin(1.0 * t + 0.5),
    ]


def odom_circle(
    t: float,
    radius: float = 2.0,
    speed: float = 0.35,
    base_z: float = STANDING_BASE_Z,
) -> Dict:
    """Body walks a circle in odom frame (x/y translate; z = standing height)."""
    omega = speed / max(radius, 1e-3)
    yaw = omega * t
    x = radius * math.sin(yaw)
    y = radius * (1.0 - math.cos(yaw))
    z = float(base_z)
    qx, qy, qz, qw = yaw_to_quat(yaw)
    vx = speed * math.cos(yaw)
    vy = speed * math.sin(yaw)
    return {
        "v": 1,
        "header": {"frame_id": ODOM_FRAME, "stamp_ms": int(t * 1000)},
        "child_frame_id": BASE_FRAME,
        "pose": {
            "position": {"x": x, "y": y, "z": z},
            "orientation": {"x": qx, "y": qy, "z": qz, "w": qw},
        },
        "twist": {
            "linear": {"x": vx, "y": vy, "z": 0.0},
            "angular": {"x": 0.0, "y": 0.0, "z": omega},
        },
    }


def joint_state_msg(names: Sequence[str], positions: Sequence[float], t_ms: int) -> Dict:
    return {
        "v": 1,
        "header": {"frame_id": "", "stamp_ms": t_ms},
        "name": list(names),
        "position": [float(p) for p in positions],
        "velocity": [0.0] * len(names),
        "effort": [0.0] * len(names),
    }


def remap_leg_to_urdf(driver_names: Sequence[str], positions: Sequence[float]) -> Tuple[List[str], List[float]]:
    names: List[str] = []
    pos: List[float] = []
    for n, p in zip(driver_names, positions):
        names.append(DRIVER_TO_URDF.get(n, n))
        pos.append(float(p))
    return names, pos
