"""G3 onboard forwarder (domain 0 MVP): ROS2 → Zenoh/TCP + cmd reverse."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Optional

from xgc2_b2_link.codec import (
    high_level_cmd,
    now_ms,
    pack_cdr_ros2,
    pack_json,
    power_summary_from_low_state_fields,
    unpack_json,
)
from xgc2_b2_link.contract import full_key, load_contract
from xgc2_b2_link.rate import RateGate
from xgc2_b2_link.transport import open_transport


def _try_import_rclpy():
    try:
        import rclpy
        from rclpy.node import Node
        from rclpy.qos import qos_profile_sensor_data
        from std_msgs.msg import String

        return rclpy, Node, qos_profile_sensor_data, String
    except ImportError:
        return None, None, None, None


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="XGC2 B2 onboard forwarder (G3)")
    p.add_argument("--robot-id", required=True)
    p.add_argument("--transport", default="auto", choices=["auto", "zenoh", "tcp"])
    p.add_argument("--tcp-host", default="127.0.0.1")
    p.add_argument("--tcp-port", type=int, default=7448)
    p.add_argument("--tcp-role", default="client", choices=["client", "server"])
    p.add_argument("--zenoh-listen", action="append", default=[])
    p.add_argument("--zenoh-connect", action="append", default=[])
    p.add_argument("--enable-imu", action="store_true")
    p.add_argument("--enable-odin-odom", action="store_true")
    p.add_argument(
        "--allow-cmd-types",
        default="echo_value,set_plan_goal,cancel_plan,set_motion_enable",
        help="comma-separated high-level cmd types (no cmd_vel by default)",
    )
    p.add_argument("--dry-run-no-ros", action="store_true", help="run without rclpy (contract/self-test)")
    return p


class ForwarderCore:
    """Transport + rate logic independent of ROS so unit tests can drive it."""

    def __init__(
        self,
        robot_id: str,
        transport,
        *,
        enable_imu: bool = False,
        enable_odin_odom: bool = False,
        allow_cmd_types: Optional[list] = None,
        on_command=None,
    ) -> None:
        self.robot_id = robot_id
        self.transport = transport
        self.contract = load_contract()
        self.prefix_tpl = self.contract.get("key_prefix_template", "xgc2/{robot_id}")
        self.allow_cmd_types = allow_cmd_types or ["echo_value"]
        self.on_command = on_command
        self.gates: Dict[str, RateGate] = {}
        up = self.contract["channels"]["up"]
        defaults = {
            "odom": True,
            "joint_states": True,
            "imu": enable_imu,
            "power_summary": True,
            "driver_status": True,
            "odin_odom": enable_odin_odom,
            "forwarder_hb": True,
        }
        self.enabled = defaults
        for name, spec in up.items():
            if name not in defaults:
                continue
            hz = float(spec.get("default_max_hz", 10))
            self.gates[name] = RateGate(hz)
        cmd_rel = self.contract["channels"]["down"]["cmd"]["key"]
        self.cmd_key = full_key(robot_id, cmd_rel, self.prefix_tpl)
        self.transport.subscribe(self.cmd_key, self._on_cmd_bytes)
        # also wildcard for debug
        self.transport.subscribe(full_key(robot_id, "down/**", self.prefix_tpl), self._on_cmd_bytes)

    def key(self, relative: str) -> str:
        return full_key(self.robot_id, relative, self.prefix_tpl)

    def publish_channel(self, name: str, payload: bytes) -> bool:
        if not self.enabled.get(name, False):
            return False
        gate = self.gates.get(name)
        if gate is not None and not gate.allow():
            return False
        rel = self.contract["channels"]["up"][name]["key"]
        self.transport.put(self.key(rel), payload)
        return True

    def publish_odom_cdr(self, cdr: bytes) -> bool:
        return self.publish_channel("odom", pack_cdr_ros2("nav_msgs/msg/Odometry", cdr))

    def publish_joint_cdr(self, cdr: bytes) -> bool:
        return self.publish_channel("joint_states", pack_cdr_ros2("sensor_msgs/msg/JointState", cdr))

    def publish_imu_cdr(self, cdr: bytes) -> bool:
        return self.publish_channel("imu", pack_cdr_ros2("sensor_msgs/msg/Imu", cdr))

    def publish_driver_status_cdr(self, cdr: bytes) -> bool:
        return self.publish_channel(
            "driver_status", pack_cdr_ros2("diagnostic_msgs/msg/DiagnosticArray", cdr)
        )

    def publish_power_summary(self, fields: Dict[str, Any]) -> bool:
        body = power_summary_from_low_state_fields(**fields)
        return self.publish_channel("power_summary", pack_json(body))

    def publish_hb(self, domain: int = 0) -> bool:
        body = {
            "v": 1,
            "t_ms": now_ms(),
            "robot_id": self.robot_id,
            "domain": domain,
            "channels": [k for k, v in self.enabled.items() if v],
        }
        return self.publish_channel("forwarder_hb", pack_json(body))

    def _on_cmd_bytes(self, key: str, payload: bytes) -> None:
        if key != self.cmd_key and not key.endswith("/down/cmd"):
            return
        try:
            obj = unpack_json(payload)
        except Exception:
            return
        cmd_type = str(obj.get("type", ""))
        if cmd_type not in self.allow_cmd_types:
            return
        if self.on_command:
            self.on_command(obj)


def run_ros_node(args: argparse.Namespace) -> int:
    rclpy, Node, qos_profile_sensor_data, String = _try_import_rclpy()
    if rclpy is None:
        print("rclpy not available; use --dry-run-no-ros or install ROS 2", file=sys.stderr)
        return 2

    from rclpy.serialization import serialize_message
    from nav_msgs.msg import Odometry
    from sensor_msgs.msg import Imu, JointState
    from diagnostic_msgs.msg import DiagnosticArray

    transport = open_transport(
        kind=args.transport,
        zenoh_listen=args.zenoh_listen or None,
        zenoh_connect=args.zenoh_connect or None,
        tcp_host=args.tcp_host,
        tcp_port=args.tcp_port,
        tcp_role=args.tcp_role,
    )

    class ForwarderNode(Node):
        def __init__(self) -> None:
            super().__init__("xgc2_b2_forwarder_d0")
            allow = [x.strip() for x in args.allow_cmd_types.split(",") if x.strip()]
            self._cmd_pub = self.create_publisher(String, "/remote/high_level_cmd", 10)

            def on_cmd(obj: Dict[str, Any]) -> None:
                msg = String()
                msg.data = json.dumps(obj, separators=(",", ":"))
                self._cmd_pub.publish(msg)
                self.get_logger().info(f"cmd relayed type={obj.get('type')}")

            self.core = ForwarderCore(
                args.robot_id,
                transport,
                enable_imu=args.enable_imu,
                enable_odin_odom=args.enable_odin_odom,
                allow_cmd_types=allow,
                on_command=on_cmd,
            )
            self.create_subscription(Odometry, "/b2/odom", self._on_odom, qos_profile_sensor_data)
            self.create_subscription(
                JointState, "/b2/joint_states", self._on_joint, qos_profile_sensor_data
            )
            if args.enable_imu:
                self.create_subscription(Imu, "/b2/imu", self._on_imu, qos_profile_sensor_data)
            self.create_subscription(
                DiagnosticArray, "/b2/driver_status", self._on_driver, qos_profile_sensor_data
            )
            # low_state is custom; try generic subscription via topic_type if package present
            self._try_low_state()
            self.create_timer(1.0, lambda: self.core.publish_hb(0))
            self.get_logger().info(
                f"forwarder d0 robot_id={args.robot_id} transport={args.transport}"
            )

        def _try_low_state(self) -> None:
            try:
                from b2_ros2_driver.msg import LowState  # type: ignore

                self.create_subscription(LowState, "/b2/low_state", self._on_low_state, 10)
                self.get_logger().info("subscribed /b2/low_state")
            except Exception as exc:
                self.get_logger().warn(
                    f"LowState type unavailable ({exc}); power_summary disabled until msg package is present"
                )

        def _on_odom(self, msg) -> None:
            self.core.publish_odom_cdr(serialize_message(msg))

        def _on_joint(self, msg) -> None:
            self.core.publish_joint_cdr(serialize_message(msg))

        def _on_imu(self, msg) -> None:
            self.core.publish_imu_cdr(serialize_message(msg))

        def _on_driver(self, msg) -> None:
            self.core.publish_driver_status_cdr(serialize_message(msg))

        def _on_low_state(self, msg) -> None:
            soc = None
            try:
                soc = int(msg.bms_state.soc)
            except Exception:
                pass
            self.core.publish_power_summary(
                {
                    "soc": soc,
                    "power_v": float(getattr(msg, "power_v", 0.0)),
                    "power_a": float(getattr(msg, "power_a", 0.0)),
                    "temperature_ntc1": getattr(msg, "temperature_ntc1", None),
                    "temperature_ntc2": getattr(msg, "temperature_ntc2", None),
                }
            )

    rclpy.init()
    node = ForwarderNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        transport.close()
        rclpy.shutdown()
    return 0


def run_dry(args: argparse.Namespace) -> int:
    """No ROS: publish synthetic frames for G4 loopback test."""
    import time

    transport = open_transport(
        kind=args.transport,
        zenoh_listen=args.zenoh_listen or None,
        zenoh_connect=args.zenoh_connect or None,
        tcp_host=args.tcp_host,
        tcp_port=args.tcp_port,
        tcp_role=args.tcp_role,
    )
    received = []

    def on_cmd(obj):
        received.append(obj)
        print("CMD", obj, flush=True)

    core = ForwarderCore(
        args.robot_id,
        transport,
        enable_imu=args.enable_imu,
        allow_cmd_types=[x.strip() for x in args.allow_cmd_types.split(",") if x.strip()],
        on_command=on_cmd,
    )
    print(f"dry-run forwarder robot_id={args.robot_id} transport={args.transport}", flush=True)
    for i in range(5):
        # minimal fake CDR body (not valid ROS CDR — ground peer will still pass envelope)
        core.publish_odom_cdr(b"\x00" * 16)
        core.publish_joint_cdr(b"\x00" * 16)
        core.publish_power_summary({"soc": 80 - i, "power_v": 48.0, "power_a": -0.5})
        core.publish_hb(0)
        time.sleep(0.2)
    time.sleep(0.5)
    transport.close()
    print(f"dry-run done cmds_received={len(received)}", flush=True)
    return 0


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.dry_run_no_ros:
        return run_dry(args)
    return run_ros_node(args)


if __name__ == "__main__":
    sys.exit(main())
