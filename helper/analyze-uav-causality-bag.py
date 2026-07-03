#!/usr/bin/env python3
import argparse
import bisect
import csv
import json
import math
import statistics
from pathlib import Path

import rosbag


def stamp_to_sec(stamp):
    if stamp is None:
        return None
    return float(stamp.secs) + float(stamp.nsecs) * 1.0e-9


def msg_time(topic_time, msg):
    header = getattr(msg, "header", None)
    if header is not None:
        value = stamp_to_sec(getattr(header, "stamp", None))
        if value is not None and value > 0.0 and math.isfinite(value):
            return value
    return topic_time.to_sec()


def pos_tuple(position):
    return (float(position.x), float(position.y), float(position.z))


def quat_tuple(orientation):
    return (
        float(orientation.x),
        float(orientation.y),
        float(orientation.z),
        float(orientation.w),
    )


def norm3(value):
    return math.sqrt(value[0] * value[0] + value[1] * value[1] + value[2] * value[2])


def sub3(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def quat_angle(a, b):
    dot = abs(a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3])
    dot = min(1.0, max(-1.0, dot))
    return 2.0 * math.acos(dot)


def quat_rotate(q, v):
    x, y, z, w = q
    vx, vy, vz = v
    # q * [v, 0] * q^-1, with geometry_msgs xyzw ordering.
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return (
        vx + w * tx + y * tz - z * ty,
        vy + w * ty + z * tx - x * tz,
        vz + w * tz + x * ty - y * tx,
    )


def quat_inverse_rotate(q, v):
    x, y, z, w = q
    return quat_rotate((-x, -y, -z, w), v)


def cross3(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


class TimeSeries:
    def __init__(self):
        self.times = []
        self.values = []

    def append(self, time_sec, value):
        if time_sec is None or not math.isfinite(time_sec):
            return
        self.times.append(float(time_sec))
        self.values.append(value)

    def nearest(self, time_sec, max_dt):
        if not self.times:
            return None
        index = bisect.bisect_left(self.times, time_sec)
        candidates = []
        if index < len(self.times):
            candidates.append(index)
        if index > 0:
            candidates.append(index - 1)
        best_index = min(candidates, key=lambda i: abs(self.times[i] - time_sec))
        dt = abs(self.times[best_index] - time_sec)
        if dt > max_dt:
            return None
        return self.times[best_index], self.values[best_index], dt

    def interpolate_vector(self, time_sec, key, max_gap):
        if not self.times:
            return None
        index = bisect.bisect_left(self.times, time_sec)
        if 0 < index < len(self.times):
            t0 = self.times[index - 1]
            t1 = self.times[index]
            if t0 <= time_sec <= t1 and time_sec - t0 <= max_gap and t1 - time_sec <= max_gap:
                v0 = self.values[index - 1].get(key)
                v1 = self.values[index].get(key)
                if v0 is not None and v1 is not None:
                    if t1 <= t0:
                        return v0
                    ratio = (time_sec - t0) / (t1 - t0)
                    return tuple(v0[i] + ratio * (v1[i] - v0[i]) for i in range(3))
        nearest = self.nearest(time_sec, max_gap)
        if nearest is None:
            return None
        return nearest[1].get(key)


def first_event(rows, name, predicate):
    for row in rows:
        if predicate(row):
            return {"name": name, "time": row["t"], "row": row}
    return None


def add_event(events, rows, name, predicate):
    event = first_event(rows, name, predicate)
    if event is not None:
        events.append(event)


def select_gazebo_model(names, ns, requested):
    if requested and requested != "auto":
        return requested if requested in names else None
    preferred = [ns, f"{ns}::base_link", f"{ns}::fs150"]
    for name in preferred:
        if name in names:
            return name
    for name in names:
        lowered = name.lower()
        if name != "ground_plane" and (ns.lower() in lowered or "uav" in lowered or "fs150" in lowered):
            return name
    for name in names:
        if name != "ground_plane":
            return name
    return None


def read_bag(path, ns, model_name):
    topics = {
        "state": f"/{ns}/alg/state_estimator/state",
        "nmpc": f"/{ns}/alg/nmpc/debug_sample",
        "vrpn_pose": f"/vrpn_client_node/{ns}/pose",
        "vrpn_twist": f"/vrpn_client_node/{ns}/twist",
        "gazebo": "/gazebo/model_states",
        "attitude_target": f"/{ns}/mavros/setpoint_raw/attitude",
        "local_pose": f"/{ns}/mavros/local_position/pose",
        "local_velocity": f"/{ns}/mavros/local_position/velocity_local",
        "imu_raw": f"/{ns}/mavros/imu/data_raw",
        "imu": f"/{ns}/mavros/imu/data",
    }
    series = {
        "state": TimeSeries(),
        "nmpc": TimeSeries(),
        "vrpn_pose": TimeSeries(),
        "vrpn_twist": TimeSeries(),
        "gazebo": TimeSeries(),
        "attitude_target": TimeSeries(),
        "local_pose": TimeSeries(),
        "local_velocity": TimeSeries(),
        "imu_raw": TimeSeries(),
        "imu": TimeSeries(),
    }

    selected_gazebo_model = None

    with rosbag.Bag(str(path), "r") as bag:
        for topic, msg, topic_time in bag.read_messages(topics=list(topics.values())):
            t = msg_time(topic_time, msg)
            if topic == topics["state"]:
                series["state"].append(
                    t,
                    {
                        "pos": pos_tuple(msg.position),
                        "vel": pos_tuple(msg.velocity),
                        "quat": quat_tuple(msg.orientation),
                        "omega": pos_tuple(msg.angular_velocity),
                        "linear_acceleration": pos_tuple(msg.linear_acceleration),
                        "pos_innov": float(msg.last_pose_position_innovation_norm_m),
                        "ori_innov": float(msg.last_pose_orientation_innovation_norm_rad),
                        "nis": float(msg.last_pose_mahalanobis_distance),
                        "flags": int(msg.flags),
                        "vrpn_state": int(msg.vrpn_observation_state),
                        "filter_health": int(msg.filter_health),
                        "reject_reason": int(msg.last_pose_reject_reason),
                        "accepted": bool(msg.last_pose_accepted),
                        "rejects": int(msg.vrpn_consecutive_rejects),
                        "filter_inertial_time": float(msg.filter_inertial_stamp_sec),
                        "filter_pose_time": float(msg.filter_pose_stamp_sec),
                        "last_vrpn_pose_time": float(msg.last_vrpn_pose_stamp_sec),
                    },
                )
            elif topic == topics["nmpc"]:
                reference_alpha = getattr(msg, "reference_alpha", None)
                series["nmpc"].append(
                    t,
                    {
                        "state_pos": pos_tuple(msg.state_pose.position),
                        "state_vel": pos_tuple(msg.state_twist.linear),
                        "state_omega": pos_tuple(msg.state_twist.angular),
                        "state_quat": quat_tuple(msg.state_pose.orientation),
                        "ref_pos": pos_tuple(msg.reference_pose.position),
                        "ref_vel": pos_tuple(msg.reference_twist.linear),
                        "ref_omega": pos_tuple(msg.reference_twist.angular),
                        "ref_quat": quat_tuple(msg.reference_pose.orientation),
                        "ref_accel": pos_tuple(msg.reference_acceleration),
                        "pos_err": pos_tuple(msg.position_error),
                        "vel_err": pos_tuple(msg.velocity_error),
                        "omega_err": pos_tuple(msg.omega_error),
                        "body_rate_cmd": pos_tuple(msg.body_rate_command),
                        "predicted_body_rate": pos_tuple(msg.predicted_body_rate),
                        "alpha_cmd": pos_tuple(msg.alpha_command),
                        "reference_alpha": pos_tuple(reference_alpha)
                        if reference_alpha is not None
                        else (float("nan"), float("nan"), float("nan")),
                        "thrust_raw": float(msg.normalized_thrust_raw),
                        "thrust_cmd": float(msg.normalized_thrust_command),
                        "specific_thrust": float(msg.specific_thrust_command),
                        "success": bool(msg.success),
                        "status": int(msg.solver_status),
                        "solve_ms": float(msg.solve_time_ms),
                        "state_estimate_time": float(msg.state_estimate_stamp_sec),
                        "filter_inertial_time": float(msg.filter_inertial_stamp_sec),
                        "filter_pose_time": float(msg.filter_pose_stamp_sec),
                        "last_vrpn_pose_time": float(msg.last_vrpn_pose_stamp_sec),
                        "sat_thrust_min": bool(msg.normalized_thrust_min_saturated),
                        "sat_thrust_max": bool(msg.normalized_thrust_max_saturated),
                        "sat_rate": bool(
                            msg.roll_rate_saturated
                            or msg.pitch_rate_saturated
                            or msg.yaw_rate_saturated
                        ),
                        "sat_alpha": bool(
                            msg.roll_alpha_saturated
                            or msg.pitch_alpha_saturated
                            or msg.yaw_alpha_saturated
                        ),
                    },
                )
            elif topic == topics["vrpn_pose"]:
                series["vrpn_pose"].append(
                    t,
                    {
                        "pos": pos_tuple(msg.pose.position),
                        "quat": quat_tuple(msg.pose.orientation),
                    },
                )
            elif topic == topics["vrpn_twist"]:
                series["vrpn_twist"].append(
                    t,
                    {
                        "vel": pos_tuple(msg.twist.linear),
                        "omega": pos_tuple(msg.twist.angular),
                    },
                )
            elif topic == topics["gazebo"]:
                if selected_gazebo_model is None:
                    selected_gazebo_model = select_gazebo_model(msg.name, ns, model_name)
                if selected_gazebo_model in msg.name:
                    index = msg.name.index(selected_gazebo_model)
                    series["gazebo"].append(
                        t,
                        {
                            "pos": pos_tuple(msg.pose[index].position),
                            "quat": quat_tuple(msg.pose[index].orientation),
                            "vel": pos_tuple(msg.twist[index].linear),
                            "omega_world": pos_tuple(msg.twist[index].angular),
                        },
                    )
            elif topic == topics["attitude_target"]:
                series["attitude_target"].append(
                    t,
                    {
                        "body_rate": pos_tuple(msg.body_rate),
                        "thrust": float(msg.thrust),
                    },
                )
            elif topic == topics["local_pose"]:
                series["local_pose"].append(
                    t,
                    {
                        "pos": pos_tuple(msg.pose.position),
                        "quat": quat_tuple(msg.pose.orientation),
                    },
                )
            elif topic == topics["local_velocity"]:
                series["local_velocity"].append(
                    t,
                    {
                        "vel": pos_tuple(msg.twist.linear),
                    },
                )
            elif topic == topics["imu_raw"] or topic == topics["imu"]:
                key = "imu_raw" if topic == topics["imu_raw"] else "imu"
                series[key].append(
                    t,
                    {
                        "angular_velocity": pos_tuple(msg.angular_velocity),
                        "linear_acceleration": pos_tuple(msg.linear_acceleration),
                    },
                )
    series["gazebo_model_name"] = selected_gazebo_model
    return series


def build_rows(series, max_sync_dt):
    all_times = []
    for key in ("state", "nmpc", "vrpn_pose", "gazebo"):
        all_times.extend(series[key].times)
    if not all_times:
        return [], 0.0
    t0 = min(all_times)

    state_rows = []
    for time_sec, state in zip(series["state"].times, series["state"].values):
        state_time = state["filter_inertial_time"] if state["filter_inertial_time"] > 0.0 else time_sec
        row = {
            "t": time_sec - t0,
            "source": "state",
            "state_age": time_sec - state_time,
            "state_z": state["pos"][2],
            "state_speed": norm3(state["vel"]),
            "state_vertical_velocity": state["vel"][2],
            "state_angular_rate": norm3(state["omega"]),
            "state_linear_acceleration": norm3(state["linear_acceleration"]),
            "pos_innov": state["pos_innov"],
            "ori_innov": state["ori_innov"],
            "nis": state["nis"],
            "flags": state["flags"],
            "vrpn_state": state["vrpn_state"],
            "filter_health": state["filter_health"],
            "rejects": state["rejects"],
            "est_vrpn_pos_err": float("nan"),
            "est_gazebo_pos_err": float("nan"),
            "vrpn_gazebo_pos_err": float("nan"),
            "est_vrpn_ori_err": float("nan"),
            "est_gazebo_ori_err": float("nan"),
            "est_vrpn_vel_err": float("nan"),
            "est_gazebo_vel_err": float("nan"),
            "state_imu_raw_omega_err": float("nan"),
            "state_imu_omega_err": float("nan"),
            "state_gazebo_body_omega_err": float("nan"),
            "state_vrpn_omega_err": float("nan"),
        }
        vrpn_pos = series["vrpn_pose"].interpolate_vector(state_time, "pos", max_sync_dt)
        gazebo_pos = series["gazebo"].interpolate_vector(state_time, "pos", max_sync_dt)
        vrpn = series["vrpn_pose"].nearest(state_time, max_sync_dt)
        gazebo = series["gazebo"].nearest(state_time, max_sync_dt)
        vrpn_vel = series["vrpn_twist"].interpolate_vector(state_time, "vel", max_sync_dt)
        gazebo_vel = series["gazebo"].interpolate_vector(state_time, "vel", max_sync_dt)
        if vrpn_pos is not None:
            row["est_vrpn_pos_err"] = norm3(sub3(state["pos"], vrpn_pos))
            if vrpn is not None:
                row["est_vrpn_ori_err"] = quat_angle(state["quat"], vrpn[1]["quat"])
        if gazebo_pos is not None:
            row["est_gazebo_pos_err"] = norm3(sub3(state["pos"], gazebo_pos))
            if gazebo is not None:
                row["est_gazebo_ori_err"] = quat_angle(state["quat"], gazebo[1]["quat"])
        if vrpn_vel is not None:
            row["est_vrpn_vel_err"] = norm3(sub3(state["vel"], vrpn_vel))
        if gazebo_vel is not None:
            row["est_gazebo_vel_err"] = norm3(sub3(state["vel"], gazebo_vel))
        if vrpn_pos is not None and gazebo_pos is not None:
            row["vrpn_gazebo_pos_err"] = norm3(sub3(vrpn_pos, gazebo_pos))
        imu_raw = series["imu_raw"].nearest(state_time, max_sync_dt)
        if imu_raw is not None:
            row["state_imu_raw_omega_err"] = norm3(
                sub3(state["omega"], imu_raw[1]["angular_velocity"])
            )
        imu = series["imu"].nearest(state_time, max_sync_dt)
        if imu is not None:
            row["state_imu_omega_err"] = norm3(sub3(state["omega"], imu[1]["angular_velocity"]))
        if gazebo is not None and "omega_world" in gazebo[1]:
            gazebo_body_omega = quat_inverse_rotate(gazebo[1]["quat"], gazebo[1]["omega_world"])
            row["state_gazebo_body_omega_err"] = norm3(sub3(state["omega"], gazebo_body_omega))
        vrpn_twist = series["vrpn_twist"].nearest(state_time, max_sync_dt)
        if vrpn_twist is not None:
            row["state_vrpn_omega_err"] = norm3(sub3(state["omega"], vrpn_twist[1]["omega"]))
        state_rows.append(row)

    nmpc_rows = []
    for time_sec, debug in zip(series["nmpc"].times, series["nmpc"].values):
        row = {
            "t": time_sec - t0,
            "source": "nmpc",
            "state_age": time_sec - debug["filter_inertial_time"]
            if debug["filter_inertial_time"] > 0.0
            else float("nan"),
            "nmpc_state_x": debug["state_pos"][0],
            "nmpc_state_y": debug["state_pos"][1],
            "nmpc_state_z": debug["state_pos"][2],
            "nmpc_ref_x": debug["ref_pos"][0],
            "nmpc_ref_y": debug["ref_pos"][1],
            "nmpc_ref_z": debug["ref_pos"][2],
            "nmpc_state_vx": debug["state_vel"][0],
            "nmpc_state_vy": debug["state_vel"][1],
            "nmpc_state_vz": debug["state_vel"][2],
            "nmpc_ref_vx": debug["ref_vel"][0],
            "nmpc_ref_vy": debug["ref_vel"][1],
            "nmpc_ref_vz": debug["ref_vel"][2],
            "nmpc_pos_err": norm3(debug["pos_err"]),
            "nmpc_pos_err_x": debug["pos_err"][0],
            "nmpc_pos_err_y": debug["pos_err"][1],
            "nmpc_pos_err_z": debug["pos_err"][2],
            "nmpc_vel_err": norm3(debug["vel_err"]),
            "nmpc_vel_err_x": debug["vel_err"][0],
            "nmpc_vel_err_y": debug["vel_err"][1],
            "nmpc_vel_err_z": debug["vel_err"][2],
            "nmpc_omega_err": norm3(debug["omega_err"]),
            "nmpc_omega_err_x": debug["omega_err"][0],
            "nmpc_omega_err_y": debug["omega_err"][1],
            "nmpc_omega_err_z": debug["omega_err"][2],
            "nmpc_attitude_error": quat_angle(debug["state_quat"], debug["ref_quat"]),
            "nmpc_thrust_direction_error": norm3(
                cross3(
                    quat_rotate(debug["ref_quat"], (0.0, 0.0, 1.0)),
                    quat_rotate(debug["state_quat"], (0.0, 0.0, 1.0)),
                )
            ),
            "_ref_quat": debug["ref_quat"],
            "state_angular_rate": norm3(debug["state_omega"]),
            "state_omega_x": debug["state_omega"][0],
            "state_omega_y": debug["state_omega"][1],
            "state_omega_z": debug["state_omega"][2],
            "ref_angular_rate": norm3(debug["ref_omega"]),
            "ref_omega_x": debug["ref_omega"][0],
            "ref_omega_y": debug["ref_omega"][1],
            "ref_omega_z": debug["ref_omega"][2],
            "ref_acceleration": norm3(debug["ref_accel"]),
            "ref_acceleration_x": debug["ref_accel"][0],
            "ref_acceleration_y": debug["ref_accel"][1],
            "ref_acceleration_z": debug["ref_accel"][2],
            "body_rate_command_norm": norm3(debug["body_rate_cmd"]),
            "body_rate_command_x": debug["body_rate_cmd"][0],
            "body_rate_command_y": debug["body_rate_cmd"][1],
            "body_rate_command_z": debug["body_rate_cmd"][2],
            "predicted_body_rate_norm": norm3(debug["predicted_body_rate"]),
            "predicted_body_rate_x": debug["predicted_body_rate"][0],
            "predicted_body_rate_y": debug["predicted_body_rate"][1],
            "predicted_body_rate_z": debug["predicted_body_rate"][2],
            "body_rate_predicted_delta": norm3(
                sub3(debug["body_rate_cmd"], debug["predicted_body_rate"])
            ),
            "alpha_command_norm": norm3(debug["alpha_cmd"]),
            "alpha_command_x": debug["alpha_cmd"][0],
            "alpha_command_y": debug["alpha_cmd"][1],
            "alpha_command_z": debug["alpha_cmd"][2],
            "reference_alpha_norm": norm3(debug["reference_alpha"]),
            "reference_alpha_x": debug["reference_alpha"][0],
            "reference_alpha_y": debug["reference_alpha"][1],
            "reference_alpha_z": debug["reference_alpha"][2],
            "alpha_reference_delta": norm3(sub3(debug["alpha_cmd"], debug["reference_alpha"])),
            "thrust_raw": debug["thrust_raw"],
            "thrust_cmd": debug["thrust_cmd"],
            "specific_thrust": debug["specific_thrust"],
            "solve_ms": debug["solve_ms"],
            "success": debug["success"],
            "status": debug["status"],
            "sat_thrust_min": debug["sat_thrust_min"],
            "sat_thrust_max": debug["sat_thrust_max"],
            "sat_rate": debug["sat_rate"],
            "sat_alpha": debug["sat_alpha"],
            "nmpc_state_vrpn_pos_err": float("nan"),
            "nmpc_state_gazebo_pos_err": float("nan"),
            "nmpc_current_vrpn_pos_err": float("nan"),
            "nmpc_current_gazebo_pos_err": float("nan"),
            "nmpc_state_vrpn_vel_err": float("nan"),
            "nmpc_state_gazebo_vel_err": float("nan"),
            "nmpc_current_vrpn_vel_err": float("nan"),
            "nmpc_current_gazebo_vel_err": float("nan"),
            "attitude_target_body_rate_norm": float("nan"),
            "attitude_target_thrust": float("nan"),
            "attitude_target_delta": float("nan"),
            "imu_raw_angular_rate": float("nan"),
            "imu_raw_linear_acceleration": float("nan"),
            "imu_angular_rate": float("nan"),
            "imu_linear_acceleration": float("nan"),
            "nmpc_state_imu_raw_omega_err": float("nan"),
            "nmpc_state_imu_omega_err": float("nan"),
            "nmpc_state_gazebo_body_omega_err": float("nan"),
            "nmpc_state_vrpn_omega_err": float("nan"),
        }
        state_time = debug["filter_inertial_time"] if debug["filter_inertial_time"] > 0.0 else time_sec
        vrpn_pos = series["vrpn_pose"].interpolate_vector(state_time, "pos", max_sync_dt)
        gazebo_pos = series["gazebo"].interpolate_vector(state_time, "pos", max_sync_dt)
        if vrpn_pos is not None:
            row["nmpc_state_vrpn_pos_err"] = norm3(sub3(debug["state_pos"], vrpn_pos))
        if gazebo_pos is not None:
            row["nmpc_state_gazebo_pos_err"] = norm3(sub3(debug["state_pos"], gazebo_pos))
        vrpn_vel = series["vrpn_twist"].interpolate_vector(state_time, "vel", max_sync_dt)
        gazebo_vel = series["gazebo"].interpolate_vector(state_time, "vel", max_sync_dt)
        if vrpn_vel is not None:
            row["nmpc_state_vrpn_vel_err"] = norm3(sub3(debug["state_vel"], vrpn_vel))
        if gazebo_vel is not None:
            row["nmpc_state_gazebo_vel_err"] = norm3(sub3(debug["state_vel"], gazebo_vel))
        current_vrpn_pos = series["vrpn_pose"].interpolate_vector(time_sec, "pos", max_sync_dt)
        current_gazebo_pos = series["gazebo"].interpolate_vector(time_sec, "pos", max_sync_dt)
        current_vrpn_vel = series["vrpn_twist"].interpolate_vector(time_sec, "vel", max_sync_dt)
        current_gazebo_vel = series["gazebo"].interpolate_vector(time_sec, "vel", max_sync_dt)
        if current_vrpn_pos is not None:
            row["nmpc_current_vrpn_pos_err"] = norm3(sub3(debug["state_pos"], current_vrpn_pos))
        if current_gazebo_pos is not None:
            row["nmpc_current_gazebo_pos_err"] = norm3(sub3(debug["state_pos"], current_gazebo_pos))
        if current_vrpn_vel is not None:
            row["nmpc_current_vrpn_vel_err"] = norm3(sub3(debug["state_vel"], current_vrpn_vel))
        if current_gazebo_vel is not None:
            row["nmpc_current_gazebo_vel_err"] = norm3(sub3(debug["state_vel"], current_gazebo_vel))
        attitude_target = series["attitude_target"].nearest(time_sec, max_sync_dt)
        if attitude_target is not None:
            row["attitude_target_body_rate_norm"] = norm3(attitude_target[1]["body_rate"])
            row["attitude_target_body_rate_x"] = attitude_target[1]["body_rate"][0]
            row["attitude_target_body_rate_y"] = attitude_target[1]["body_rate"][1]
            row["attitude_target_body_rate_z"] = attitude_target[1]["body_rate"][2]
            row["attitude_target_thrust"] = attitude_target[1]["thrust"]
            row["attitude_target_delta"] = norm3(
                sub3(debug["body_rate_cmd"], attitude_target[1]["body_rate"])
            )
        imu_raw = series["imu_raw"].nearest(state_time, max_sync_dt)
        if imu_raw is not None:
            row["imu_raw_angular_rate"] = norm3(imu_raw[1]["angular_velocity"])
            row["imu_raw_angular_rate_x"] = imu_raw[1]["angular_velocity"][0]
            row["imu_raw_angular_rate_y"] = imu_raw[1]["angular_velocity"][1]
            row["imu_raw_angular_rate_z"] = imu_raw[1]["angular_velocity"][2]
            row["imu_raw_linear_acceleration"] = norm3(imu_raw[1]["linear_acceleration"])
            row["imu_raw_linear_acceleration_x"] = imu_raw[1]["linear_acceleration"][0]
            row["imu_raw_linear_acceleration_y"] = imu_raw[1]["linear_acceleration"][1]
            row["imu_raw_linear_acceleration_z"] = imu_raw[1]["linear_acceleration"][2]
            row["nmpc_state_imu_raw_omega_err"] = norm3(
                sub3(debug["state_omega"], imu_raw[1]["angular_velocity"])
            )
        imu = series["imu"].nearest(state_time, max_sync_dt)
        if imu is not None:
            row["imu_angular_rate"] = norm3(imu[1]["angular_velocity"])
            row["imu_angular_rate_x"] = imu[1]["angular_velocity"][0]
            row["imu_angular_rate_y"] = imu[1]["angular_velocity"][1]
            row["imu_angular_rate_z"] = imu[1]["angular_velocity"][2]
            row["imu_linear_acceleration"] = norm3(imu[1]["linear_acceleration"])
            row["imu_linear_acceleration_x"] = imu[1]["linear_acceleration"][0]
            row["imu_linear_acceleration_y"] = imu[1]["linear_acceleration"][1]
            row["imu_linear_acceleration_z"] = imu[1]["linear_acceleration"][2]
            row["nmpc_state_imu_omega_err"] = norm3(
                sub3(debug["state_omega"], imu[1]["angular_velocity"])
            )
        gazebo = series["gazebo"].nearest(state_time, max_sync_dt)
        if gazebo is not None and "omega_world" in gazebo[1]:
            gazebo_body_omega = quat_inverse_rotate(gazebo[1]["quat"], gazebo[1]["omega_world"])
            row["nmpc_state_gazebo_body_omega_err"] = norm3(
                sub3(debug["state_omega"], gazebo_body_omega)
            )
        vrpn_twist = series["vrpn_twist"].nearest(state_time, max_sync_dt)
        if vrpn_twist is not None:
            row["nmpc_state_vrpn_omega_err"] = norm3(
                sub3(debug["state_omega"], vrpn_twist[1]["omega"])
            )
        nmpc_rows.append(row)

    previous_nmpc = None
    for row in nmpc_rows:
        row["body_rate_command_step"] = float("nan")
        row["body_rate_command_slew"] = float("nan")
        row["alpha_command_step"] = float("nan")
        row["reference_attitude_step"] = float("nan")
        row["reference_thrust_direction_step"] = float("nan")
        if previous_nmpc is not None:
            dt = row["t"] - previous_nmpc["t"]
            if dt > 1.0e-9:
                body_rate = (
                    row["body_rate_command_x"],
                    row["body_rate_command_y"],
                    row["body_rate_command_z"],
                )
                previous_body_rate = (
                    previous_nmpc["body_rate_command_x"],
                    previous_nmpc["body_rate_command_y"],
                    previous_nmpc["body_rate_command_z"],
                )
                row["body_rate_command_step"] = norm3(sub3(body_rate, previous_body_rate))
                row["body_rate_command_slew"] = row["body_rate_command_step"] / dt
                alpha = (row["alpha_command_x"], row["alpha_command_y"], row["alpha_command_z"])
                previous_alpha = (
                    previous_nmpc["alpha_command_x"],
                    previous_nmpc["alpha_command_y"],
                    previous_nmpc["alpha_command_z"],
                )
                row["alpha_command_step"] = norm3(sub3(alpha, previous_alpha))
                row["reference_attitude_step"] = quat_angle(
                    row["_ref_quat"], previous_nmpc["_ref_quat"]
                )
                row["reference_thrust_direction_step"] = norm3(
                    cross3(
                        quat_rotate(row["_ref_quat"], (0.0, 0.0, 1.0)),
                        quat_rotate(previous_nmpc["_ref_quat"], (0.0, 0.0, 1.0)),
                    )
                )
        previous_nmpc = row

    for row in nmpc_rows:
        row.pop("_ref_quat", None)

    return {"state": state_rows, "nmpc": nmpc_rows}, t0


def finite_max(rows, key):
    values = [row[key] for row in rows if key in row and math.isfinite(row[key])]
    return max(values) if values else float("nan")


def finite_min(rows, key):
    values = [row[key] for row in rows if key in row and math.isfinite(row[key])]
    return min(values) if values else float("nan")


def median_offset(pairs, start_s, warmup_s):
    values = [offset for time_sec, offset in pairs if start_s <= time_sec <= start_s + warmup_s]
    if not values:
        values = [offset for _, offset in pairs[:100]]
    if not values:
        return (0.0, 0.0, 0.0)
    return tuple(statistics.median(offset[i] for offset in values) for i in range(3))


def offset_residual(offset, baseline):
    return norm3(sub3(offset, baseline))


def analyze(path, args):
    series = read_bag(path, args.ns, args.model_name)
    rows_by_source, t0 = build_rows(series, args.max_sync_dt)
    state_rows = rows_by_source["state"]
    nmpc_rows = rows_by_source["nmpc"]
    valid_state_rows = [
        row
        for row in state_rows
        if row["filter_health"] == 0 and row["flags"] == 0 and row["state_z"] > args.valid_start_min_z
    ]
    valid_start_t = valid_state_rows[0]["t"] if valid_state_rows else 0.0

    est_vrpn_offsets = []
    est_gazebo_offsets = []
    vrpn_gazebo_offsets = []
    for time_sec, state in zip(series["state"].times, series["state"].values):
        t = time_sec - t0
        state_time = state["filter_inertial_time"] if state["filter_inertial_time"] > 0.0 else time_sec
        vrpn_pos = series["vrpn_pose"].interpolate_vector(state_time, "pos", args.max_sync_dt)
        gazebo_pos = series["gazebo"].interpolate_vector(state_time, "pos", args.max_sync_dt)
        if vrpn_pos is not None:
            est_vrpn_offsets.append((t, sub3(state["pos"], vrpn_pos)))
        if gazebo_pos is not None:
            est_gazebo_offsets.append((t, sub3(state["pos"], gazebo_pos)))
        if vrpn_pos is not None and gazebo_pos is not None:
            vrpn_gazebo_offsets.append((t, sub3(vrpn_pos, gazebo_pos)))

    baselines = {
        "est_vrpn": median_offset(est_vrpn_offsets, valid_start_t, args.baseline_duration),
        "est_gazebo": median_offset(est_gazebo_offsets, valid_start_t, args.baseline_duration),
        "vrpn_gazebo": median_offset(vrpn_gazebo_offsets, valid_start_t, args.baseline_duration),
    }

    for row in state_rows:
        row["est_vrpn_pos_residual"] = float("nan")
        row["est_gazebo_pos_residual"] = float("nan")
        row["vrpn_gazebo_pos_residual"] = float("nan")

    state_by_t = {round(row["t"], 9): row for row in state_rows}
    for time_sec, state in zip(series["state"].times, series["state"].values):
        row = state_by_t.get(round(time_sec - t0, 9))
        if row is None:
            continue
        state_time = state["filter_inertial_time"] if state["filter_inertial_time"] > 0.0 else time_sec
        vrpn_pos = series["vrpn_pose"].interpolate_vector(state_time, "pos", args.max_sync_dt)
        gazebo_pos = series["gazebo"].interpolate_vector(state_time, "pos", args.max_sync_dt)
        if vrpn_pos is not None:
            row["est_vrpn_pos_residual"] = offset_residual(
                sub3(state["pos"], vrpn_pos), baselines["est_vrpn"]
            )
        if gazebo_pos is not None:
            row["est_gazebo_pos_residual"] = offset_residual(
                sub3(state["pos"], gazebo_pos), baselines["est_gazebo"]
            )
        if vrpn_pos is not None and gazebo_pos is not None:
            row["vrpn_gazebo_pos_residual"] = offset_residual(
                sub3(vrpn_pos, gazebo_pos), baselines["vrpn_gazebo"]
            )

    for row in nmpc_rows:
        row["nmpc_state_vrpn_pos_residual"] = float("nan")
        row["nmpc_state_gazebo_pos_residual"] = float("nan")
    nmpc_by_t = {round(row["t"], 9): row for row in nmpc_rows}
    for time_sec, debug in zip(series["nmpc"].times, series["nmpc"].values):
        row = nmpc_by_t.get(round(time_sec - t0, 9))
        if row is None:
            continue
        state_time = debug["filter_inertial_time"] if debug["filter_inertial_time"] > 0.0 else time_sec
        vrpn_pos = series["vrpn_pose"].interpolate_vector(state_time, "pos", args.max_sync_dt)
        gazebo_pos = series["gazebo"].interpolate_vector(state_time, "pos", args.max_sync_dt)
        if vrpn_pos is not None:
            row["nmpc_state_vrpn_pos_residual"] = offset_residual(
                sub3(debug["state_pos"], vrpn_pos), baselines["est_vrpn"]
            )
        if gazebo_pos is not None:
            row["nmpc_state_gazebo_pos_residual"] = offset_residual(
                sub3(debug["state_pos"], gazebo_pos), baselines["est_gazebo"]
            )

    event_state_rows = [row for row in state_rows if row["t"] >= valid_start_t]
    event_nmpc_rows = [row for row in nmpc_rows if row["t"] >= valid_start_t]

    events = []
    add_event(events, event_state_rows, "state_pos_innovation_gt_0p10m", lambda r: r["pos_innov"] > 0.10)
    add_event(events, event_state_rows, "state_ori_innovation_gt_0p10rad", lambda r: r["ori_innov"] > 0.10)
    add_event(events, event_state_rows, "state_estimator_flags_nonzero", lambda r: r["flags"] != 0)
    add_event(
        events,
        event_state_rows,
        "state_posterior_vs_vrpn_residual_gt_0p03m",
        lambda r: r["est_vrpn_pos_residual"] > 0.03,
    )
    add_event(
        events,
        event_state_rows,
        "state_posterior_vs_gazebo_residual_gt_0p03m",
        lambda r: r["est_gazebo_pos_residual"] > 0.03,
    )
    add_event(
        events,
        event_state_rows,
        "raw_vrpn_vs_gazebo_residual_gt_0p03m",
        lambda r: r["vrpn_gazebo_pos_residual"] > 0.03,
    )
    add_event(
        events,
        event_state_rows,
        "raw_vrpn_vs_gazebo_residual_gt_0p10m",
        lambda r: r["vrpn_gazebo_pos_residual"] > 0.10,
    )
    add_event(
        events,
        event_state_rows,
        "state_imu_raw_omega_error_gt_0p20radps",
        lambda r: r["state_imu_raw_omega_err"] > 0.20,
    )
    add_event(
        events,
        event_state_rows,
        "state_gazebo_body_omega_error_gt_0p20radps",
        lambda r: r["state_gazebo_body_omega_err"] > 0.20,
    )
    add_event(events, event_state_rows, "state_low_z_after_25s", lambda r: r["t"] > 25.0 and r["state_z"] < 0.6)
    add_event(events, event_nmpc_rows, "nmpc_pos_error_gt_0p30m", lambda r: r["nmpc_pos_err"] > 0.30)
    add_event(events, event_nmpc_rows, "nmpc_vel_error_gt_1p00mps", lambda r: r["nmpc_vel_err"] > 1.00)
    add_event(events, event_nmpc_rows, "nmpc_body_rate_cmd_gt_0p30radps", lambda r: r["body_rate_command_norm"] > 0.30)
    add_event(events, event_nmpc_rows, "nmpc_body_rate_step_gt_0p20radps", lambda r: r["body_rate_command_step"] > 0.20)
    add_event(events, event_nmpc_rows, "nmpc_body_rate_slew_gt_10radps2", lambda r: r["body_rate_command_slew"] > 10.0)
    add_event(events, event_nmpc_rows, "nmpc_alpha_norm_gt_5radps2", lambda r: r["alpha_command_norm"] > 5.0)
    add_event(events, event_nmpc_rows, "nmpc_alpha_saturated", lambda r: r["sat_alpha"])
    add_event(events, event_nmpc_rows, "nmpc_thrust_min_saturated", lambda r: r["sat_thrust_min"])
    add_event(events, event_nmpc_rows, "nmpc_thrust_max_saturated", lambda r: r["sat_thrust_max"])
    add_event(events, event_nmpc_rows, "nmpc_solver_failed", lambda r: not r["success"] or r["status"] != 0)
    add_event(events, event_nmpc_rows, "nmpc_state_age_gt_0p05s", lambda r: r["state_age"] > 0.05)
    add_event(events, event_nmpc_rows, "imu_raw_angular_rate_gt_0p30radps", lambda r: r["imu_raw_angular_rate"] > 0.30)
    add_event(
        events,
        event_nmpc_rows,
        "nmpc_state_imu_raw_omega_error_gt_0p20radps",
        lambda r: r["nmpc_state_imu_raw_omega_err"] > 0.20,
    )
    add_event(
        events,
        event_nmpc_rows,
        "nmpc_state_gazebo_body_omega_error_gt_0p20radps",
        lambda r: r["nmpc_state_gazebo_body_omega_err"] > 0.20,
    )
    add_event(events, event_nmpc_rows, "mavros_setpoint_mismatch_gt_0p05radps", lambda r: r["attitude_target_delta"] > 0.05)
    events.sort(key=lambda event: event["time"])

    severe_names = {
        "state_pos_innovation_gt_0p10m",
        "state_ori_innovation_gt_0p10rad",
        "state_low_z_after_25s",
        "nmpc_pos_error_gt_0p30m",
        "nmpc_vel_error_gt_1p00mps",
        "nmpc_thrust_min_saturated",
        "nmpc_thrust_max_saturated",
        "nmpc_solver_failed",
        "raw_vrpn_vs_gazebo_residual_gt_0p10m",
    }
    failure_onset = next((event for event in events if event["name"] in severe_names), None)
    if failure_onset is not None:
        causal_start_t = max(valid_start_t, failure_onset["time"] - 5.0)
        causal_events = [event for event in events if event["time"] >= causal_start_t]
    else:
        causal_start_t = valid_start_t
        causal_events = events

    classification_events = [
        event
        for event in causal_events
        if event["name"]
        not in {
            "raw_vrpn_vs_gazebo_residual_gt_0p03m",
            "state_posterior_vs_vrpn_residual_gt_0p03m",
            "state_posterior_vs_gazebo_residual_gt_0p03m",
            "mavros_setpoint_mismatch_gt_0p05radps",
        }
    ]
    first_names = [event["name"] for event in classification_events[:5]]
    if failure_onset is None:
        classification = "normal_or_no_severe_failure"
    elif any(name == "raw_vrpn_vs_gazebo_residual_gt_0p10m" for name in first_names[:2]):
        classification = "measurement_layer_first"
    elif any(name.startswith("state_posterior") for name in first_names[:2]):
        classification = "estimator_posterior_first"
    elif any(name.startswith("nmpc_") for name in first_names[:2]):
        classification = "control_or_vehicle_dynamics_first"
    elif any(
        name.startswith("state_ori_innovation") or name.startswith("state_pos_innovation")
        for name in first_names[:2]
    ):
        classification = "eskf_prior_residual_first"
    else:
        classification = "no_clear_failure_or_insufficient_data"

    summary = {
        "bag": str(path),
        "t0": t0,
        "valid_start_t": valid_start_t,
        "gazebo_model_name": series.get("gazebo_model_name"),
        "samples": {
            key: len(value.times)
            for key, value in series.items()
            if isinstance(value, TimeSeries)
        },
        "max_pos_innovation": finite_max(state_rows, "pos_innov"),
        "max_ori_innovation": finite_max(state_rows, "ori_innov"),
        "max_est_vrpn_pos_err": finite_max(state_rows, "est_vrpn_pos_err"),
        "max_est_gazebo_pos_err": finite_max(state_rows, "est_gazebo_pos_err"),
        "max_est_vrpn_vel_err": finite_max(state_rows, "est_vrpn_vel_err"),
        "max_est_gazebo_vel_err": finite_max(state_rows, "est_gazebo_vel_err"),
        "max_vrpn_gazebo_pos_err": finite_max(state_rows, "vrpn_gazebo_pos_err"),
        "max_est_vrpn_pos_residual": finite_max(state_rows, "est_vrpn_pos_residual"),
        "max_est_gazebo_pos_residual": finite_max(state_rows, "est_gazebo_pos_residual"),
        "max_vrpn_gazebo_pos_residual": finite_max(state_rows, "vrpn_gazebo_pos_residual"),
        "max_state_imu_raw_omega_err": finite_max(state_rows, "state_imu_raw_omega_err"),
        "max_state_imu_omega_err": finite_max(state_rows, "state_imu_omega_err"),
        "max_state_gazebo_body_omega_err": finite_max(state_rows, "state_gazebo_body_omega_err"),
        "max_state_vrpn_omega_err": finite_max(state_rows, "state_vrpn_omega_err"),
        "max_nmpc_pos_err": finite_max(nmpc_rows, "nmpc_pos_err"),
        "max_nmpc_vel_err": finite_max(nmpc_rows, "nmpc_vel_err"),
        "max_nmpc_attitude_error": finite_max(nmpc_rows, "nmpc_attitude_error"),
        "max_nmpc_thrust_direction_error": finite_max(nmpc_rows, "nmpc_thrust_direction_error"),
        "max_reference_attitude_step": finite_max(nmpc_rows, "reference_attitude_step"),
        "max_reference_thrust_direction_step": finite_max(
            nmpc_rows, "reference_thrust_direction_step"
        ),
        "max_nmpc_body_rate_command": finite_max(nmpc_rows, "body_rate_command_norm"),
        "max_nmpc_body_rate_step": finite_max(nmpc_rows, "body_rate_command_step"),
        "max_nmpc_body_rate_slew": finite_max(nmpc_rows, "body_rate_command_slew"),
        "max_nmpc_alpha_command": finite_max(nmpc_rows, "alpha_command_norm"),
        "max_reference_alpha": finite_max(nmpc_rows, "reference_alpha_norm"),
        "max_alpha_reference_delta": finite_max(nmpc_rows, "alpha_reference_delta"),
        "max_imu_raw_angular_rate": finite_max(nmpc_rows, "imu_raw_angular_rate"),
        "max_mavros_setpoint_delta": finite_max(nmpc_rows, "attitude_target_delta"),
        "max_nmpc_state_vrpn_pos_residual": finite_max(nmpc_rows, "nmpc_state_vrpn_pos_residual"),
        "max_nmpc_state_gazebo_pos_residual": finite_max(nmpc_rows, "nmpc_state_gazebo_pos_residual"),
        "max_nmpc_state_vrpn_vel_err": finite_max(nmpc_rows, "nmpc_state_vrpn_vel_err"),
        "max_nmpc_state_gazebo_vel_err": finite_max(nmpc_rows, "nmpc_state_gazebo_vel_err"),
        "max_nmpc_state_imu_raw_omega_err": finite_max(nmpc_rows, "nmpc_state_imu_raw_omega_err"),
        "max_nmpc_state_imu_omega_err": finite_max(nmpc_rows, "nmpc_state_imu_omega_err"),
        "max_nmpc_state_gazebo_body_omega_err": finite_max(
            nmpc_rows, "nmpc_state_gazebo_body_omega_err"
        ),
        "max_nmpc_state_vrpn_omega_err": finite_max(nmpc_rows, "nmpc_state_vrpn_omega_err"),
        "max_state_age": finite_max(state_rows, "state_age"),
        "max_nmpc_state_age": finite_max(nmpc_rows, "state_age"),
        "max_solve_ms": finite_max(nmpc_rows, "solve_ms"),
        "min_state_z_after_25s": finite_min([r for r in state_rows if r["t"] > 25.0], "state_z"),
        "position_baselines": baselines,
        "classification": classification,
        "failure_onset_time": failure_onset["time"] if failure_onset is not None else None,
        "failure_onset_name": failure_onset["name"] if failure_onset is not None else None,
        "causal_window_start_time": causal_start_t,
        "events": [
            {
                "name": event["name"],
                "time": event["time"],
                "row": {
                    key: value
                    for key, value in event["row"].items()
                    if isinstance(value, (bool, int, float, str))
                },
            }
            for event in events
        ],
        "causal_events": [
            {
                "name": event["name"],
                "time": event["time"],
                "row": {
                    key: value
                    for key, value in event["row"].items()
                    if isinstance(value, (bool, int, float, str))
                },
            }
            for event in causal_events[:32]
        ],
    }
    return summary, rows_by_source


def write_outputs(summary, rows_by_source, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "analysis.json").write_text(json.dumps(summary, indent=2, sort_keys=True))

    with (output_dir / "events.tsv").open("w", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(["time", "name", "source"])
        for event in summary["events"]:
            writer.writerow([f"{event['time']:.6f}", event["name"], event["row"].get("source", "")])

    for source, rows in rows_by_source.items():
        if not rows:
            continue
        keys = sorted({key for row in rows for key in row.keys()})
        with (output_dir / f"{source}_metrics.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(rows)


def parse_args():
    parser = argparse.ArgumentParser(description="Analyze UAV NMPC/ESKF/Gazebo causality bags.")
    parser.add_argument("bag", type=Path)
    parser.add_argument("--ns", default="uav1")
    parser.add_argument("--model-name", default="uav1")
    parser.add_argument("--max-sync-dt", type=float, default=0.03)
    parser.add_argument("--baseline-duration", type=float, default=5.0)
    parser.add_argument("--valid-start-min-z", type=float, default=0.5)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def main():
    args = parse_args()
    output_dir = args.output_dir
    if output_dir is None:
        output_dir = args.bag.with_suffix("")
    summary, rows_by_source = analyze(args.bag, args)
    write_outputs(summary, rows_by_source, output_dir)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
