#!/usr/bin/env python3
import argparse
import math
import sys

import rospy
from mavros_msgs.msg import State
from std_msgs.msg import String

from estimator_vrpn_px4_rotor_state.msg import RigidStateEstimate


class AutoTakeoffTrack:
    def __init__(self, args):
        self.args = args
        self.state = None
        self.estimate = None
        self.command_pub = rospy.Publisher(args.command_topic, String, queue_size=10)
        rospy.Subscriber(f"/{args.ns}/mavros/state", State, self._state_cb, queue_size=1)
        rospy.Subscriber(
            f"/{args.ns}/alg/state_estimator/state",
            RigidStateEstimate,
            self._estimate_cb,
            queue_size=1,
        )

    def _state_cb(self, msg):
        self.state = msg

    def _estimate_cb(self, msg):
        self.estimate = msg

    def wait_ready(self):
        deadline = rospy.Time.now() + rospy.Duration(self.args.ready_timeout)
        rate = rospy.Rate(10.0)
        while not rospy.is_shutdown() and rospy.Time.now() < deadline:
            connected = self.state is not None and self.state.connected
            estimate_ok = self.estimate is not None and math.isfinite(self.estimate.position.z)
            command_ok = self.command_pub.get_num_connections() > 0
            if connected and estimate_ok and command_ok:
                return True
            rate.sleep()
        return False

    def publish_command(self, command, duration):
        msg = String(data=command)
        deadline = rospy.Time.now() + rospy.Duration(duration)
        rate = rospy.Rate(self.args.command_rate)
        while not rospy.is_shutdown() and rospy.Time.now() < deadline:
            self.command_pub.publish(msg)
            rate.sleep()

    def wait_altitude(self):
        deadline = rospy.Time.now() + rospy.Duration(self.args.takeoff_timeout)
        rate = rospy.Rate(10.0)
        threshold = self.args.height - self.args.altitude_margin
        while not rospy.is_shutdown() and rospy.Time.now() < deadline:
            if self.estimate is not None and math.isfinite(self.estimate.position.z):
                z = self.estimate.position.z
                vz = self.estimate.velocity.z if math.isfinite(self.estimate.velocity.z) else 0.0
                if z >= threshold and abs(vz) <= self.args.max_settle_vz:
                    return True
            if self.state is not None and not self.state.armed:
                self.publish_command("takeoff", 0.5)
            rate.sleep()
        return False

    def run(self):
        if not self.wait_ready():
            rospy.logerr("[uav_auto] timed out waiting for MAVROS, estimator, and /command")
            return 2

        rospy.loginfo("[uav_auto] publishing takeoff")
        self.publish_command("takeoff", self.args.command_duration)

        if not self.wait_altitude():
            z = self.estimate.position.z if self.estimate is not None else float("nan")
            rospy.logerr("[uav_auto] takeoff altitude not reached: z=%.3f target=%.3f", z, self.args.height)
            return 3

        rospy.sleep(self.args.post_takeoff_hold)
        rospy.loginfo("[uav_auto] publishing custom1")
        self.publish_command("custom1", self.args.command_duration)
        rospy.loginfo("[uav_auto] auto takeoff-track command sequence complete")
        return 0


def parse_args():
    parser = argparse.ArgumentParser(description="Auto command UAV takeoff then custom1 tracking.")
    parser.add_argument("--ns", default="uav1")
    parser.add_argument("--height", type=float, default=3.0)
    parser.add_argument("--command-topic", default="/command")
    parser.add_argument("--ready-timeout", type=float, default=60.0)
    parser.add_argument("--takeoff-timeout", type=float, default=60.0)
    parser.add_argument("--altitude-margin", type=float, default=0.25)
    parser.add_argument("--max-settle-vz", type=float, default=0.6)
    parser.add_argument("--post-takeoff-hold", type=float, default=1.0)
    parser.add_argument("--command-rate", type=float, default=2.0)
    parser.add_argument("--command-duration", type=float, default=3.0)
    return parser.parse_args()


def main():
    args = parse_args()
    rospy.init_node("uav_auto_takeoff_track", anonymous=True, disable_signals=True)
    return AutoTakeoffTrack(args).run()


if __name__ == "__main__":
    sys.exit(main())
