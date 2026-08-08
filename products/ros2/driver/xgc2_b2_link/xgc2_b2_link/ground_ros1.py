"""ROS1 recovery publishers for G4 ground peer (Noetic)."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

from xgc2_b2_link.sim_models import (
    ARM_URDF_JOINTS,
    BASE_FRAME,
    DRIVER_TO_URDF,
    ODOM_FRAME,
    remap_leg_to_urdf,
)


class Ros1RecoveredPubs:
    def __init__(self) -> None:
        import rospy
        from geometry_msgs.msg import PoseStamped, TransformStamped
        from nav_msgs.msg import Odometry, Path
        from sensor_msgs.msg import JointState
        from std_msgs.msg import String
        import tf2_ros

        self.rospy = rospy
        if not rospy.core.is_initialized():
            rospy.init_node("xgc2_b2_ground_peer", anonymous=False, disable_signals=True)
        self.Odometry = Odometry
        self.JointState = JointState
        self.Path = Path
        self.PoseStamped = PoseStamped
        self.String = String
        self.TransformStamped = TransformStamped

        self.pub_odom = rospy.Publisher("/remote/b2/odom", Odometry, queue_size=20)
        self.pub_joint = rospy.Publisher("/remote/b2/joint_states", JointState, queue_size=20)
        # robot_state_publisher default
        self.pub_joint_rsp = rospy.Publisher("/joint_states", JointState, queue_size=20)
        self.pub_path = rospy.Publisher("/remote/b2/path", Path, queue_size=10, latch=True)
        self.pub_power = rospy.Publisher("/remote/b2/power_summary", String, queue_size=10)
        self.pub_driver = rospy.Publisher("/remote/b2/driver_status_json", String, queue_size=10)
        self.pub_arm = rospy.Publisher("/remote/arm/slave_joint_states", JointState, queue_size=20)
        self.tf_broadcaster = tf2_ros.TransformBroadcaster()

        self._path = Path()
        self._path.header.frame_id = ODOM_FRAME
        self._leg_pos: Dict[str, float] = {}
        self._arm_pos: Dict[str, float] = {}
        self._max_path = 2000

    def handle(self, channel: str, meta: Dict[str, Any]) -> None:
        js = meta.get("json")
        if not isinstance(js, dict):
            return
        # channel is relative name under up/ (e.g. odom, joint_states, arm_slave_status)
        if channel == "odom":
            self._pub_odom(js)
        elif channel == "joint_states":
            self._pub_legs(js)
        elif channel == "power_summary":
            self._pub_string(self.pub_power, js)
        elif channel == "driver_status":
            self._pub_string(self.pub_driver, js)
        elif channel == "arm_slave_status":
            self._pub_arm(js)

    def _stamp(self, stamp_ms: Optional[int] = None):
        import rospy

        if stamp_ms is None:
            return rospy.Time.now()
        return rospy.Time.from_sec(stamp_ms / 1000.0)

    def _pub_string(self, pub, obj: Dict[str, Any]) -> None:
        import json

        msg = self.String()
        msg.data = json.dumps(obj, separators=(",", ":"))
        pub.publish(msg)

    def _pub_odom(self, js: Dict[str, Any]) -> None:
        o = self.Odometry()
        stamp_ms = (js.get("header") or {}).get("stamp_ms")
        o.header.stamp = self._stamp(stamp_ms)
        o.header.frame_id = (js.get("header") or {}).get("frame_id") or ODOM_FRAME
        o.child_frame_id = js.get("child_frame_id") or BASE_FRAME
        p = (js.get("pose") or {}).get("position") or {}
        q = (js.get("pose") or {}).get("orientation") or {}
        o.pose.pose.position.x = float(p.get("x", 0.0))
        o.pose.pose.position.y = float(p.get("y", 0.0))
        o.pose.pose.position.z = float(p.get("z", 0.0))
        o.pose.pose.orientation.x = float(q.get("x", 0.0))
        o.pose.pose.orientation.y = float(q.get("y", 0.0))
        o.pose.pose.orientation.z = float(q.get("z", 0.0))
        o.pose.pose.orientation.w = float(q.get("w", 1.0))
        tw = js.get("twist") or {}
        lin = tw.get("linear") or {}
        ang = tw.get("angular") or {}
        o.twist.twist.linear.x = float(lin.get("x", 0.0))
        o.twist.twist.linear.y = float(lin.get("y", 0.0))
        o.twist.twist.linear.z = float(lin.get("z", 0.0))
        o.twist.twist.angular.z = float(ang.get("z", 0.0))
        self.pub_odom.publish(o)

        # TF odom -> base
        t = self.TransformStamped()
        t.header = o.header
        t.child_frame_id = o.child_frame_id
        t.transform.translation.x = o.pose.pose.position.x
        t.transform.translation.y = o.pose.pose.position.y
        t.transform.translation.z = o.pose.pose.position.z
        t.transform.rotation = o.pose.pose.orientation
        self.tf_broadcaster.sendTransform(t)

        # Path history
        ps = self.PoseStamped()
        ps.header = o.header
        ps.pose = o.pose.pose
        self._path.header = o.header
        self._path.poses.append(ps)
        if len(self._path.poses) > self._max_path:
            self._path.poses = self._path.poses[-self._max_path :]
        self.pub_path.publish(self._path)

    def _pub_legs(self, js: Dict[str, Any]) -> None:
        names = js.get("name") or []
        pos = js.get("position") or []
        urdf_names, urdf_pos = remap_leg_to_urdf(names, pos)
        for n, p in zip(urdf_names, urdf_pos):
            self._leg_pos[n] = p
        self._publish_combined(js)

    def _pub_arm(self, js: Dict[str, Any]) -> None:
        full = js.get("joint_pos_full")
        names = js.get("joint_names") or ARM_URDF_JOINTS
        if full is not None:
            positions = list(full)
        else:
            positions = list(js.get("joint_pos") or [])
            # pad prismatic if only 6
            while len(positions) < len(names):
                positions.append(0.0)
        for n, p in zip(names, positions):
            self._arm_pos[n] = float(p)
        # dedicated arm topic
        msg = self.JointState()
        msg.header.stamp = self._stamp(js.get("t_ms"))
        msg.name = list(names[: len(positions)])
        msg.position = [float(x) for x in positions[: len(msg.name)]]
        self.pub_arm.publish(msg)
        self._publish_combined(js)

    def _publish_combined(self, js: Dict[str, Any]) -> None:
        msg = self.JointState()
        msg.header.stamp = self._stamp(js.get("t_ms") or (js.get("header") or {}).get("stamp_ms"))
        names = list(self._leg_pos.keys()) + list(self._arm_pos.keys())
        positions = [self._leg_pos[n] for n in self._leg_pos] + [self._arm_pos[n] for n in self._arm_pos]
        # stable order: sorted legs then arms by known lists
        ordered: List[str] = []
        ordered_pos: List[float] = []
        for n in DRIVER_TO_URDF.values():
            if n in self._leg_pos:
                ordered.append(n)
                ordered_pos.append(self._leg_pos[n])
        for n in ARM_URDF_JOINTS:
            if n in self._arm_pos:
                ordered.append(n)
                ordered_pos.append(self._arm_pos[n])
        if ordered:
            msg.name = ordered
            msg.position = ordered_pos
        else:
            msg.name = names
            msg.position = positions
        self.pub_joint.publish(msg)
        self.pub_joint_rsp.publish(msg)
