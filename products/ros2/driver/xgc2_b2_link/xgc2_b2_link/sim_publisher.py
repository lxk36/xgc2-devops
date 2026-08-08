"""G3-side scene simulator: publishes odom/joints/power/arm over shared transport.

Uses JSON payloads (same keys as Zenoh contract) so host-only ROS1 ground recovery
can rebuild standard messages without ROS2 CDR.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from typing import List

from xgc2_b2_link.codec import arm_status_json, now_ms, pack_json, power_summary_from_low_state_fields
from xgc2_b2_link.contract import full_key, load_contract
from xgc2_b2_link.rate import RateGate
from xgc2_b2_link.sim_models import (
    ARM_URDF_JOINTS,
    DRIVER_LEG_JOINTS,
    arm_positions,
    joint_state_msg,
    odom_circle,
    walk_leg_positions,
)
from xgc2_b2_link.transport import open_transport


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Simulate B2+R5a telemetry onto G3→G4 transport")
    p.add_argument("--robot-id", default="b2-sim")
    p.add_argument("--transport", default="tcp", choices=["auto", "zenoh", "tcp"])
    p.add_argument("--tcp-host", default="127.0.0.1")
    p.add_argument("--tcp-port", type=int, default=7448)
    p.add_argument("--tcp-role", default="client", choices=["client", "server"])
    p.add_argument("--hz", type=float, default=30.0, help="sim physics rate")
    p.add_argument("--duration", type=float, default=0.0, help="0 = forever")
    p.add_argument("--no-arm", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    contract = load_contract()
    tpl = contract.get("key_prefix_template", "xgc2/{robot_id}")
    transport = open_transport(
        kind=args.transport,
        tcp_host=args.tcp_host,
        tcp_port=args.tcp_port,
        tcp_role=args.tcp_role,
    )

    def k(rel: str) -> str:
        return full_key(args.robot_id, rel, tpl)

    gates = {
        "odom": RateGate(15),
        "joint": RateGate(15),
        "power": RateGate(2),
        "driver": RateGate(1),
        "arm": RateGate(15),
        "hb": RateGate(1),
    }

    print(
        f"sim_publisher robot_id={args.robot_id} transport={args.transport} "
        f"tcp={args.tcp_host}:{args.tcp_port}/{args.tcp_role}",
        flush=True,
    )
    t0 = time.monotonic()
    period = 1.0 / max(args.hz, 1.0)
    n = 0
    try:
        while True:
            t = time.monotonic() - t0
            t_ms = now_ms()
            if gates["odom"].allow():
                odom = odom_circle(t)
                odom["header"]["stamp_ms"] = t_ms
                transport.put(k("up/odom"), pack_json(odom))
            if gates["joint"].allow():
                legs = walk_leg_positions(t)
                js = joint_state_msg(DRIVER_LEG_JOINTS, legs, t_ms)
                # dual naming: driver names on wire; ground remaps to URDF
                transport.put(k("up/joint_states"), pack_json(js))
            if gates["power"].allow():
                soc = int(70 + 10 * math.sin(0.05 * t))
                body = power_summary_from_low_state_fields(
                    soc=soc, power_v=48.0 - 0.5 * math.sin(0.1 * t), power_a=-1.2, t_ms=t_ms
                )
                transport.put(k("up/power_summary"), pack_json(body))
            if gates["driver"].allow():
                # lightweight JSON stand-in for DiagnosticArray
                transport.put(
                    k("up/driver_status"),
                    pack_json(
                        {
                            "v": 1,
                            "t_ms": t_ms,
                            "status": "OK",
                            "motion_enabled": True,
                            "command_stale": False,
                        }
                    ),
                )
            if not args.no_arm and gates["arm"].allow():
                pos = arm_positions(t)
                # full 8 for URDF; status schema uses joint_pos list
                arm_body = arm_status_json(
                    pos[:6],
                    joint_vel=[0.0] * 6,
                    joint_cur=[0.0] * 6,
                    end_pos=[0.3, 0.0, 0.4, 0.0, 0.0, 0.0],
                    t_ms=t_ms,
                )
                arm_body["joint_names"] = ARM_URDF_JOINTS
                arm_body["joint_pos_full"] = pos
                transport.put(k("up/arm_slave_status"), pack_json(arm_body))
            if gates["hb"].allow():
                transport.put(
                    k("up/forwarder_hb"),
                    pack_json(
                        {
                            "v": 1,
                            "t_ms": t_ms,
                            "robot_id": args.robot_id,
                            "domain": 0,
                            "channels": ["odom", "joint_states", "power_summary", "arm_slave_status"],
                            "mode": "sim",
                        }
                    ),
                )
            n += 1
            if args.duration > 0 and t >= args.duration:
                break
            time.sleep(period)
    except KeyboardInterrupt:
        pass
    finally:
        transport.close()
    print(f"sim_publisher stopped frames≈{n}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
