"""Bounded household demonstrations using measured simulation state.

The official HSR geometry is controlled with Gazebo's simple controllers.
Grasping is explicitly assisted attachment, not contact/friction grasping.
"""
from collections import deque
import json
import logging
import math
from pathlib import Path
import threading
import time
import xml.etree.ElementTree as ET

import numpy as np
from scipy.spatial.transform import Rotation

import rclpy
from rclpy.context import Context
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import JointState
from std_msgs.msg import Empty, Float64, String
from geometry_msgs.msg import Twist
from tf2_msgs.msg import TFMessage
from rosgraph_msgs.msg import Clock

from sim_runtime import ROOT, configure_environment

ACTIONS = {
    "greet", "nod", "look_ball", "open_hand", "close_hand", "raise_arm", "home",
    "pick_cube", "place_cube", "pick_ball", "place_ball",
    "forward", "backward", "turn_left", "turn_right", "stop",
}


class Cancelled(Exception):
    pass


class Kinematics:
    def __init__(self, path):
        root = ET.parse(path).getroot()
        self.parents = {}
        for joint in root.findall("joint"):
            origin = joint.find("origin")
            xyz = [float(v) for v in origin.get("xyz", "0 0 0").split()] if origin is not None else [0]*3
            rpy = [float(v) for v in origin.get("rpy", "0 0 0").split()] if origin is not None else [0]*3
            axis = joint.find("axis")
            vector = [float(v) for v in axis.get("xyz", "1 0 0").split()] if axis is not None else [1, 0, 0]
            transform = np.eye(4)
            transform[:3, :3] = Rotation.from_euler("xyz", rpy).as_matrix()
            transform[:3, 3] = xyz
            self.parents[joint.find("child").get("link")] = (
                joint.find("parent").get("link"), joint.get("name"),
                joint.get("type"), transform, np.asarray(vector, dtype=float),
            )

    def transform(self, link, values):
        if link not in self.parents:
            return np.eye(4)
        parent, name, kind, origin, axis = self.parents[link]
        motion = np.eye(4)
        value = values.get(name, 0.0)
        if kind in ("revolute", "continuous"):
            motion[:3, :3] = Rotation.from_rotvec(axis * value).as_matrix()
        elif kind == "prismatic":
            motion[:3, 3] = axis * value
        return self.transform(parent, values) @ origin @ motion


class HouseholdController:
    def __init__(self):
        configure_environment()
        self.manifest = json.loads((ROOT / "assets/manifest.json").read_text())
        self.kinematics = Kinematics(ROOT / "assets/robot.urdf")
        self.limits = self.manifest["controlled_joints"]
        self.home_pose = self.manifest["home"]
        self.grasp_link = self.manifest.get("grasp_link", "hand_palm_link")
        self.grasp_offset = np.array([*self.manifest.get("grasp_offset", [0, 0, .085]), 1.0])
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.shutdown_event = threading.Event()
        self.joints = {}
        self.poses = {}
        self.pose_updates = {}
        self.attach_state = {}
        self.pending_grasp = None
        self.attachment_uncertain = False
        self.last_joint_update = 0.0
        self.last_pose_update = 0.0
        self.last_clock_update = 0.0
        self.busy = False
        self.current_action = None
        self.holding = None
        self.message = "シミュレーションを準備しています。"
        self.error = None
        self.history = deque(maxlen=30)
        self.worker = None
        self.initialized = False
        self.context = Context()
        rclpy.init(context=self.context)
        self.node = Node("household_controller", context=self.context)
        self.publishers = {
            name: self.node.create_publisher(Float64, f"/household/joint/{name}/cmd", 10)
            for name in self.limits
        }
        self.base_pub = self.node.create_publisher(Twist, "/household/cmd_vel", 10)
        self.grasp_pubs = {}
        for item in ("cube", "ball"):
            for verb in ("attach", "detach"):
                self.grasp_pubs[item, verb] = self.node.create_publisher(
                    Empty, f"/household/grasp/{item}/{verb}", 10
                )
            self.node.create_subscription(
                String, f"/household/grasp/{item}/state",
                lambda msg, key=item: self._attach_callback(key, msg),
                10,
            )
        self.node.create_subscription(JointState, "/joint_states", self._joint_callback, qos_profile_sensor_data)
        for topic in ("/household/robot_poses", "/household/cube/poses", "/household/ball/poses"):
            self.node.create_subscription(TFMessage, topic, self._pose_callback, qos_profile_sensor_data)
        self.node.create_subscription(Clock, "/clock", self._clock_callback, qos_profile_sensor_data)
        self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)
        self.spin_thread = threading.Thread(target=self._spin, daemon=True)
        self.spin_thread.start()
        self.init_thread = threading.Thread(target=self._initialize, daemon=True)
        self.init_thread.start()

    def _joint_callback(self, msg):
        with self.lock:
            self.joints.update(zip(msg.name, msg.position))
            self.last_joint_update = time.monotonic()

    def _pose_callback(self, msg):
        with self.lock:
            for transform in msg.transforms:
                name = transform.child_frame_id.rstrip("/").split("::")[-1].split("/")[-1]
                if name not in ("hsr", "cube", "ball"):
                    continue
                value = transform.transform
                quat = [value.rotation.x, value.rotation.y, value.rotation.z, value.rotation.w]
                matrix = np.eye(4)
                if np.linalg.norm(quat) > 0.01:
                    matrix[:3, :3] = Rotation.from_quat(quat).as_matrix()
                matrix[:3, 3] = [value.translation.x, value.translation.y, value.translation.z]
                self.poses[name] = matrix
                self.pose_updates[name] = time.monotonic()
            self.last_pose_update = time.monotonic()

    def _clock_callback(self, msg):
        with self.lock:
            self.last_clock_update = time.monotonic()

    def _attach_callback(self, item, msg):
        with self.lock:
            self.attach_state[item] = msg.data
            if self.pending_grasp == (item, "attach" if msg.data == "attached" else "detach"):
                self.pending_grasp = None
            # The world starts with detachable joints attached. Startup explicitly
            # detaches both before enabling commands, so don't report those as picks.
            if self.initialized:
                attached = [name for name, state in self.attach_state.items() if state == "attached"]
                self.attachment_uncertain = len(attached) > 1
                self.holding = attached[0] if len(attached) == 1 else None
                if self.attachment_uncertain:
                    self.error = "複数の物体が接続されています。アプリケーションを再起動してください。"

    def _spin(self):
        while not self.shutdown_event.is_set() and self.context.ok():
            try:
                self.executor.spin_once(timeout_sec=.1)
            except Exception:
                if not self.shutdown_event.is_set():
                    logging.getLogger(__name__).exception("Household ROS executor failed")
                    with self.lock:
                        self.error = "ロボットとの通信が停止しました。ログを確認してください。"
                break

    def _lookup_pose(self, name):
        with self.lock:
            if name in self.poses:
                return self.poses[name].copy()
            for key, value in self.poses.items():
                if key.rstrip("/").split("::")[-1].split("/")[-1] == name:
                    return value.copy()
        raise RuntimeError(f"{name} の位置をまだ取得できていません。")

    def _fresh(self):
        now = time.monotonic()
        return (
            now - self.last_joint_update < 2 and
            now - self.last_pose_update < 2 and
            now - self.last_clock_update < 2
            and all(now - self.pose_updates.get(name, 0) < 2 for name in ("hsr", "cube", "ball"))
        )

    def _initialize(self):
        deadline = time.monotonic() + 60
        discovery_ready_since = None
        while not self.shutdown_event.is_set() and time.monotonic() < deadline:
            with self.lock:
                if self.cancel.is_set():
                    self.message = "準備中に停止しました。操作を始めるにはアプリケーションを再起動してください。"
                    return
                discovered = self._fresh() and all(
                    self.grasp_pubs[item, "detach"].get_subscription_count() > 0
                    and self.node.count_publishers(f"/household/grasp/{item}/state") > 0
                    for item in ("cube", "ball")
                )
                if discovered:
                    if discovery_ready_since is None:
                        discovery_ready_since = time.monotonic()
                else:
                    discovery_ready_since = None
                discovery_settled = (
                    discovery_ready_since is not None
                    and time.monotonic() - discovery_ready_since >= .3
                )
                # DetachableJoint publishes on state transitions only. Do not
                # detach before both bridge directions and state readers exist.
                if discovery_settled:
                    for item in ("cube", "ball"):
                        self.grasp_pubs[item, "detach"].publish(Empty())
                ready = discovery_settled and all(
                    self.attach_state.get(item) == "detached" for item in ("cube", "ball")
                )
            if ready:
                try:
                    self._lookup_pose("hsr")
                    self._lookup_pose("cube")
                    self._lookup_pose("ball")
                    self._move_joints(self.home_pose, timeout=25)
                    with self.lock:
                        self._check()
                        self.initialized = True
                        self.holding = None
                        self.message = "準備できました。指示を選ぶか、入力してください。"
                except Cancelled:
                    self.stop()
                    with self.lock:
                        self.message = "準備中に停止しました。操作を始めるにはアプリケーションを再起動してください。"
                except Exception as error:
                    self.stop()
                    with self.lock:
                        self.error = str(error)
                        self.message = "初期姿勢の準備に失敗しました。アプリケーションを再起動してください。"
                return
            time.sleep(.1)
        if not self.shutdown_event.is_set():
            self.error = "起動待ちが時間切れになりました。logs フォルダを確認してください。"

    def get_state(self):
        with self.lock:
            objects = {}
            robot_pose = None
            if "hsr" in self.poses:
                base = self.poses["hsr"]
                robot_pose = {
                    "position": base[:3, 3].round(4).tolist(),
                    "yaw": round(float(Rotation.from_matrix(base[:3, :3]).as_euler("xyz")[2]), 4),
                }
            for item in ("cube", "ball"):
                try:
                    objects[item] = self._lookup_pose(item)[:3, 3].round(4).tolist()
                except RuntimeError:
                    pass
            return {
                "ready": self.initialized and self._fresh(),
                "busy": self.busy,
                "current_action": self.current_action,
                "message": self.message,
                "error": self.error,
                "holding": self.holding,
                "object_positions": objects,
                "robot_pose": robot_pose,
                "joints": {k: round(v, 4) for k, v in self.joints.items() if k in self.limits},
                "history": list(self.history),
                "grasp_mode": "assisted",
                "perception_mode": "registered_object_positions",
                "attach_state": dict(self.attach_state),
                "attachment_pending": self.pending_grasp,
                "attachment_uncertain": self.attachment_uncertain,
            }

    def submit(self, action):
        if action == "stop":
            return self.stop()
        with self.lock:
            if action not in ACTIONS:
                return {"accepted": False, "action": action, "message": "未対応の指示です。"}
            if not self.initialized or not self._fresh():
                return {"accepted": False, "action": action, "message": "ロボットの準備ができていません。"}
            if self.busy:
                return {"accepted": False, "action": action, "message": "動作中です。先に停止するか、完了をお待ちください。"}
            if self.pending_grasp or self.attachment_uncertain:
                return {"accepted": False, "action": action, "message": "物体の保持状態を確認中です。解消しない場合はアプリケーションを再起動してください。"}
            if self.holding and action in {"home", "greet", "nod", "raise_arm", "close_hand", "open_hand", "pick_cube", "pick_ball"}:
                return {"accepted": False, "action": action, "message": "先に持っている物体を置いてください。"}
            if action.startswith("place_") and self.holding != action.split("_", 1)[1]:
                return {"accepted": False, "action": action, "message": "その物体を持っていません。"}
            self.cancel.clear()
            self.busy = True
            self.current_action = action
            self.error = None
            self.worker = threading.Thread(target=self._execute, args=(action,), daemon=True)
            self.worker.start()
            return {"accepted": True, "action": action, "message": "指示を受け付けました。"}

    def stop(self):
        with self.lock:
            self.cancel.set()
            self._hold_motion()
            self.message = "停止しました。"
        return {"accepted": True, "action": "stop", "message": "停止しました。"}

    def _hold_motion(self):
        """Call under the command lock; keep any attached object attached."""
        self.base_pub.publish(Twist())
        for name, publisher in self.publishers.items():
            if name in self.joints:
                publisher.publish(Float64(data=float(self.joints[name])))

    def _check(self):
        with self.lock:
            if self.cancel.is_set() or self.shutdown_event.is_set():
                raise Cancelled("指示により停止しました。")
            if not self._fresh():
                self._hold_motion()
                raise RuntimeError("位置情報が更新されないため停止しました。")

    def _move_joints(self, targets, timeout=20):
        targets = dict(targets)
        for name, rule in self.manifest.get("mimic_relations", {}).items():
            source = rule.get("joint", rule.get("source"))
            if source in targets:
                targets[name] = targets[source] * rule.get("multiplier", 1) + rule.get("offset", 0)
        for name, value in targets.items():
            if name not in self.limits:
                raise RuntimeError(f"Unknown joint: {name}")
            bounds = self.limits[name]
            if not bounds["lower"] - 1e-6 <= value <= bounds["upper"] + 1e-6:
                raise RuntimeError(f"関節の範囲外です: {name}={value}")
        deadline = time.monotonic() + timeout
        settled = 0
        while time.monotonic() < deadline:
            with self.lock:
                self._check()
                for name, value in targets.items():
                    self.publishers[name].publish(Float64(data=float(value)))
                matched = all(
                    name in self.joints and abs(self.joints[name] - value) <
                    (.003 if "lift" in name else .015)
                    for name, value in targets.items()
                )
            settled = settled + 1 if matched else 0
            if settled >= 4:
                return
            time.sleep(.05)
        raise RuntimeError("腕の目標姿勢に到達しなかったため停止しました。")

    def _hand(self, opening):
        mapping = self.manifest.get("gripper_mapping", {})
        targets = {}
        if mapping:
            for name, rule in mapping.items():
                targets[name] = opening * rule.get("multiplier", 1) + rule.get("offset", 0)
        else:
            for name in self.limits:
                if name in ("hand_motor_joint", "hand_l_proximal_joint", "hand_r_proximal_joint"):
                    targets[name] = opening
                elif name in ("hand_l_distal_joint", "hand_r_distal_joint"):
                    targets[name] = -opening - .087
        if not targets:
            raise RuntimeError("手の関節設定が見つかりません。")
        self._move_joints(targets)

    def _base_move(self, x, y, yaw=0.0, timeout=20):
        if not (-.8 <= x <= .18 and -.8 <= y <= .8):
            raise RuntimeError("初期デモの移動範囲を超えています。")
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                with self.lock:
                    self._check()
                    base = self._lookup_pose("hsr")
                current = Rotation.from_matrix(base[:3, :3]).as_euler("xyz")[2]
                difference = math.atan2(math.sin(yaw-current), math.cos(yaw-current))
                delta = np.array([x, y]) - base[:2, 3]
                if np.linalg.norm(delta) < .008 and abs(difference) < .025:
                    return
                body = Rotation.from_euler("z", -current).apply([*delta, 0])
                msg = Twist()
                msg.linear.x = float(np.clip(body[0] * 1.4, -.12, .12))
                msg.linear.y = float(np.clip(body[1] * 1.4, -.12, .12))
                # Fixed wheel contacts in this simplified base resist very small
                # yaw commands. Keep a bounded minimum until within tolerance.
                msg.angular.z = (
                    math.copysign(max(.22, min(.35, abs(difference) * 1.5)), difference)
                    if abs(difference) >= .025 else 0.0
                )
                with self.lock:
                    self._check()
                    self.base_pub.publish(msg)
                time.sleep(.05)
            raise RuntimeError("移動先に到達しなかったため停止しました。")
        finally:
            with self.lock:
                self.base_pub.publish(Twist())

    def _grasp_point(self, values=None, base=None):
        with self.lock:
            values = dict(values) if values is not None else dict(self.joints)
            base = base.copy() if base is not None else self._lookup_pose("hsr")
        return (base @ self.kinematics.transform(self.grasp_link, values) @ self.grasp_offset)[:3]

    def _pick_pose(self, height):
        pose = {
            "arm_lift_joint": .22, "arm_flex_joint": -math.pi/2,
            "arm_roll_joint": 0.0, "wrist_flex_joint": 0.0, "wrist_roll_joint": 0.0,
        }
        point = self._grasp_point(pose, np.eye(4))
        base_z = self._lookup_pose("hsr")[2, 3]
        pose["arm_lift_joint"] += height - point[2] - base_z
        return pose

    def _grasp(self, item, verb):
        deadline = time.monotonic() + 4
        desired = "attached" if verb == "attach" else "detached"
        while time.monotonic() < deadline:
            with self.lock:
                self._check()
                if self.attach_state.get(item) == desired and self.pending_grasp is None:
                    return
                self.pending_grasp = (item, verb)
                self.grasp_pubs[item, verb].publish(Empty())
            time.sleep(.1)
            with self.lock:
                self._check()
                if self.attach_state.get(item) == desired:
                    self.pending_grasp = None
                    return
        raise RuntimeError("物体の保持状態を確認できませんでした。")

    def _pick(self, item):
        self.message = "登録された物体の位置に合わせています。"
        self._move_joints(self.home_pose)
        self._hand(.7)
        with self.lock:
            obj = self._lookup_pose(item)[:3, 3]
            initial_height = float(obj[2])
            target_pose = self._pick_pose(initial_height)
            local_point = self._grasp_point(target_pose, np.eye(4))
        self._base_move(float(obj[0] - local_point[0]), float(obj[1] - local_point[1]))
        above = dict(target_pose)
        above["arm_lift_joint"] += .16
        self._move_joints(above)
        self._move_joints(target_pose)
        with self.lock:
            gap = float(np.linalg.norm(self._grasp_point() - self._lookup_pose(item)[:3, 3]))
        if gap > .03:
            raise RuntimeError(f"手と物体が離れているため保持しません（{gap*100:.1f}cm）。")
        self.message = "手が物体に近づきました。補助付き把持で保持します。"
        self._grasp(item, "attach")
        # Maintain a visible gap: grasp is an attachment, not friction squeezing.
        self._hand(.45)
        raised = dict(target_pose)
        raised["arm_lift_joint"] += .20
        self._move_joints(raised)
        time.sleep(.2)
        with self.lock:
            rise = float(self._lookup_pose(item)[2, 3] - initial_height)
        if rise < .12:
            raise RuntimeError("物体の上昇を確認できませんでした。")
        self.message = f"{'箱' if item == 'cube' else 'ボール'}を約{rise*100:.0f}cm持ち上げました（補助付き把持）。"

    def _place(self, item):
        target = self.manifest.get("place_positions", {}).get(item)
        if target is None:
            target = self.manifest["objects"][item]["position"]
        target = np.asarray(target, dtype=float)
        pose = self._pick_pose(float(target[2]))
        offset = self._grasp_point(pose, np.eye(4))
        self.message = "物体を置く位置に移動しています。"
        self._base_move(float(target[0] - offset[0]), float(target[1] - offset[1]))
        # Release slightly above the support surface.
        pose["arm_lift_joint"] += .015
        self._move_joints(pose)
        self._hand(.7)
        self._grasp(item, "detach")
        pose["arm_lift_joint"] += .12
        self._move_joints(pose)
        time.sleep(.5)
        with self.lock:
            actual = self._lookup_pose(item)[:3, 3]
        if np.linalg.norm(actual[:2] - target[:2]) > .09 or abs(actual[2] - target[2]) > .08:
            raise RuntimeError("指定した場所に置けたことを確認できませんでした。")
        self.message = f"{'箱' if item == 'cube' else 'ボール'}を台の上に置きました。"

    def _execute(self, action):
        status = "completed"
        self.message = "動作しています。"
        try:
            if action == "home":
                self._move_joints(self.home_pose)
            elif action == "open_hand":
                self._hand(.7)
            elif action == "close_hand":
                self._hand(0.0)
            elif action == "raise_arm":
                self._move_joints({"arm_lift_joint": .5, "arm_flex_joint": -.45})
            elif action in ("greet", "nod"):
                if action == "greet":
                    self._move_joints({"arm_lift_joint": .4, "arm_flex_joint": -.55, "wrist_flex_joint": -.7})
                    self._hand(.7)
                    for angle in (-.65, .65, -.65, 0.0):
                        self._move_joints({"wrist_roll_joint": angle})
                for angle in (-.45, -.10, -.45, -.10):
                    self._move_joints({"head_tilt_joint": angle})
            elif action == "look_ball":
                with self.lock:
                    base = self._lookup_pose("hsr")
                    ball = np.linalg.inv(base) @ np.append(self._lookup_pose("ball")[:3, 3], 1.0)
                    head = self.kinematics.transform("head_pan_link", self.joints)[:3, 3]
                direction = ball[:3] - head
                pan = math.atan2(direction[1], direction[0])
                tilt = math.atan2(direction[2], max(.01, np.linalg.norm(direction[:2]) - .02))
                self._move_joints({
                    "head_pan_joint": float(np.clip(pan, -3.84, 1.75)),
                    "head_tilt_joint": float(np.clip(tilt, -1.57, .52)),
                })
            elif action.startswith("pick_"):
                self._pick(action.split("_", 1)[1])
            elif action.startswith("place_"):
                self._place(action.split("_", 1)[1])
            elif action in ("forward", "backward", "turn_left", "turn_right"):
                with self.lock:
                    base = self._lookup_pose("hsr")
                yaw = Rotation.from_matrix(base[:3, :3]).as_euler("xyz")[2]
                x, y = base[:2, 3]
                if action in ("forward", "backward"):
                    step = .1 if action == "forward" else -.1
                    x += step*math.cos(yaw)
                    y += step*math.sin(yaw)
                else:
                    yaw += .3 if action == "turn_left" else -.3
                self._base_move(float(x), float(y), float(yaw))
            with self.lock:
                self._check()
                if self.message == "動作しています。":
                    self.message = "指示した動作が完了しました。"
        except Cancelled as error:
            status = "cancelled"
            with self.lock:
                self._hold_motion()
                self.message = str(error)
        except Exception as error:
            status = "failed"
            self.error = str(error)
            self.stop()
            self.message = str(error)
        finally:
            with self.lock:
                self.base_pub.publish(Twist())
                self.history.appendleft({
                    "action": action, "status": status,
                    "message": self.message, "time": time.time(),
                })
                self.busy = False
                self.current_action = None

    def close(self):
        self.stop()
        self.shutdown_event.set()
        for thread in (self.worker, self.init_thread, self.spin_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=3)
        self.executor.shutdown(timeout_sec=3)
        self.executor.remove_node(self.node)
        self.node.destroy_node()
        self.context.shutdown()
