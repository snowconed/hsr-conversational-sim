#!/usr/bin/env python3
"""Build a small household scene from Toyota's public HSR-B description.

Run in Ubuntu after sourcing /opt/ros/jazzy/setup.bash. No robot hardware
drivers are loaded. This demo deliberately uses ideal joint/base controllers
and proximity-gated fixed joints for assisted grasping.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET


MAIN = (
    "arm_lift_joint", "arm_flex_joint", "arm_roll_joint",
    "wrist_flex_joint", "wrist_roll_joint",
    "head_pan_joint", "head_tilt_joint", "hand_motor_joint",
)
FINGERS = (
    "hand_l_proximal_joint", "hand_r_proximal_joint",
    "hand_l_distal_joint", "hand_r_distal_joint",
)
HOME = {
    "arm_lift_joint": 0.22, "arm_flex_joint": -0.25, "arm_roll_joint": 0.0,
    "wrist_flex_joint": -1.2, "wrist_roll_joint": 0.0,
    "head_pan_joint": 0.0, "head_tilt_joint": -0.25, "hand_motor_joint": 0.7,
}
PICK = dict(HOME, arm_flex_joint=-math.pi / 2, wrist_flex_joint=0.0)
GRASP_OFFSET = [0.0, 0.0, 0.085]


def sub(parent, tag, text=None, **attrs):
    element = ET.SubElement(parent, tag, attrs)
    if text is not None:
        element.text = str(text)
    return element


def save_xml(path, tree):
    ET.indent(tree, space="  ")
    ET.ElementTree(tree).write(path, encoding="utf-8", xml_declaration=True)


def plugin(parent, library, class_name, **values):
    result = sub(parent, "plugin", filename=library, name=class_name)
    for name, value in values.items():
        sub(result, name, value)
    return result


def expand_hand(pose):
    result = dict(pose)
    motor = result.get("hand_motor_joint", 0.7)
    result["torso_lift_joint"] = 0.5 * result.get("arm_lift_joint", 0.0)
    for side in ("l", "r"):
        result[f"hand_{side}_proximal_joint"] = motor
        result[f"hand_{side}_distal_joint"] = -motor - 0.087
    return result


def mm(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)]
            for i in range(4)]


def transform(xyz=(0, 0, 0), rpy=(0, 0, 0)):
    r, p, y = rpy
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return [
        [cy*cp, cy*sp*sr-sy*cr, cy*sp*cr+sy*sr, xyz[0]],
        [sy*cp, sy*sp*sr+cy*cr, sy*sp*cr-cy*sr, xyz[1]],
        [-sp, cp*sr, cp*cr, xyz[2]],
        [0, 0, 0, 1],
    ]


def fk(robot, positions, target="hand_palm_link"):
    joints = {j.find("child").get("link"): j for j in robot.findall("joint")}
    chain = []
    current = target
    while current in joints:
        joint = joints[current]
        chain.append(joint)
        current = joint.find("parent").get("link")
    result = transform()
    for joint in reversed(chain):
        origin = joint.find("origin")
        xyz = list(map(float, origin.get("xyz", "0 0 0").split())) if origin is not None else [0]*3
        rpy = list(map(float, origin.get("rpy", "0 0 0").split())) if origin is not None else [0]*3
        result = mm(result, transform(xyz, rpy))
        value = positions.get(joint.get("name"), 0.0)
        axis = joint.find("axis")
        axis = list(map(float, axis.get("xyz", "1 0 0").split())) if axis is not None else [1, 0, 0]
        if joint.get("type") == "prismatic":
            result = mm(result, transform([value*v for v in axis]))
        elif joint.get("type") in ("revolute", "continuous"):
            x, y, z = axis
            c, s, v = math.cos(value), math.sin(value), 1-math.cos(value)
            rotation = [
                [x*x*v+c, x*y*v-z*s, x*z*v+y*s, 0],
                [x*y*v+z*s, y*y*v+c, y*z*v-x*s, 0],
                [x*z*v-y*s, y*z*v+x*s, z*z*v+c, 0], [0, 0, 0, 1]]
            result = mm(result, rotation)
    return result


def point(matrix, offset=(0, 0, 0)):
    return [sum(matrix[i][j]*offset[j] for j in range(3))+matrix[i][3] for i in range(3)]


def material(visual, color):
    mat = sub(visual, "material")
    sub(mat, "ambient", color)
    sub(mat, "diffuse", color)
    sub(mat, "specular", "0.12 0.12 0.12 1")


def shape_link(model, name, pose, size=None, radius=None, color="0.7 0.7 0.7 1", mass=0.1, collision_mask=None):
    link = sub(model, "link", name=name)
    sub(link, "pose", " ".join(map(str, pose)))
    if model.findtext("static", "false") != "true":
        inertial = sub(link, "inertial")
        sub(inertial, "mass", mass)
        tensor = sub(inertial, "inertia")
        if radius:
            inertia = [0.4*mass*radius*radius]*3
        else:
            a, b, c = size
            inertia = [mass*(b*b+c*c)/12, mass*(a*a+c*c)/12, mass*(a*a+b*b)/12]
        for tag, value in zip(("ixx", "iyy", "izz"), inertia):
            sub(tensor, tag, value)
        for tag in ("ixy", "ixz", "iyz"):
            sub(tensor, tag, 0)
    for tag in ("visual", "collision"):
        elem = sub(link, tag, name=f"{name}_{tag}")
        geometry = sub(elem, "geometry")
        shape = sub(geometry, "sphere" if radius else "box")
        sub(shape, "radius" if radius else "size", radius if radius else " ".join(map(str, size)))
        if tag == "visual":
            material(elem, color)
        else:
            surface = sub(elem, "surface")
            if collision_mask is not None:
                sub(sub(surface, "contact"), "collide_bitmask", collision_mask)
            friction = sub(sub(surface, "friction"), "ode")
            sub(friction, "mu", 0.9)
            sub(friction, "mu2", 0.9)
    return link


def make_bridge(joints, objects):
    rows = []
    def add(ros_topic, gz_topic, ros_type, gz_type, direction):
        rows.append({
            "ros_topic_name": ros_topic, "gz_topic_name": gz_topic,
            "ros_type_name": ros_type, "gz_type_name": gz_type, "direction": direction,
        })
    add("/clock", "/clock", "rosgraph_msgs/msg/Clock", "gz.msgs.Clock", "GZ_TO_ROS")
    add("/joint_states", "/model/hsr/joint_state", "sensor_msgs/msg/JointState", "gz.msgs.Model", "GZ_TO_ROS")
    add("/household/cmd_vel", "/household/cmd_vel", "geometry_msgs/msg/Twist", "gz.msgs.Twist", "ROS_TO_GZ")
    add("/household/robot_poses", "/model/hsr/pose", "tf2_msgs/msg/TFMessage", "gz.msgs.Pose_V", "GZ_TO_ROS")
    for joint in joints:
        topic = f"/household/joint/{joint}/cmd"
        add(topic, topic, "std_msgs/msg/Float64", "gz.msgs.Double", "ROS_TO_GZ")
    for name in objects:
        add(f"/household/{name}/poses", f"/model/{name}/pose",
            "tf2_msgs/msg/TFMessage", "gz.msgs.Pose_V", "GZ_TO_ROS")
        for action in ("attach", "detach"):
            topic = f"/household/grasp/{name}/{action}"
            add(topic, topic, "std_msgs/msg/Empty", "gz.msgs.Empty", "ROS_TO_GZ")
        topic = f"/household/grasp/{name}/state"
        add(topic, topic, "std_msgs/msg/String", "gz.msgs.StringMsg", "GZ_TO_ROS")
    # JSON is valid YAML and avoids an unnecessary PyYAML dependency.
    return rows


def build(root):
    root = root.resolve()
    repo = root / "vendor" / "hsrb_common"
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    if not (repo / "hsrb_description/robots/hsrb4s.urdf.xacro").exists():
        raise SystemExit("Missing vendor/hsrb_common; clone the official jazzy branch first.")
    prefix = assets / "ament"
    index = prefix / "share/ament_index/resource_index/packages"
    index.mkdir(parents=True, exist_ok=True)
    for package in ("hsrb_description", "hsrb_parts_description"):
        (index / package).touch()
        link = prefix / "share" / package
        if not link.exists():
            link.symlink_to(repo / package, target_is_directory=True)
    env = dict(os.environ)
    env["AMENT_PREFIX_PATH"] = str(prefix) + os.pathsep + env.get("AMENT_PREFIX_PATH", "")
    source = repo / "hsrb_description/robots/hsrb4s.urdf.xacro"
    result = subprocess.run(["xacro", str(source), "rviz_sim:=True"], env=env,
                            text=True, capture_output=True, check=True, timeout=60)
    robot = ET.fromstring(result.stdout)
    robot.set("name", "hsr")
    for element in list(robot):
        if element.tag == "ros2_control":
            robot.remove(element)
    # Keep official inertial, collision and material definitions. Sensors and
    # Toyota-specific plugins are not used by this small manipulation demo.
    for parent in robot.iter():
        for child in list(parent):
            if child.tag in ("plugin", "sensor", "transmission"):
                parent.remove(child)
    for mesh in robot.findall(".//mesh"):
        uri = mesh.get("filename", "")
        if uri.startswith("package://"):
            mesh.set("filename", (repo / uri[len("package://"):]).as_uri())
    controlled = set(MAIN + FINGERS + ("torso_lift_joint",))
    limits = {}
    mimic = {}
    for joint in robot.findall("joint"):
        name = joint.get("name")
        imitation = joint.find("mimic")
        if imitation is not None:
            mimic[name] = {
                "joint": imitation.get("joint"),
                "multiplier": float(imitation.get("multiplier", 1)),
                "offset": float(imitation.get("offset", 0)),
            }
            joint.remove(imitation)
        if name in controlled:
            bound = joint.find("limit")
            lower, upper = float(bound.get("lower")), float(bound.get("upper"))
            if name in FINGERS and "distal" in name:
                # The source mimic has a -0.087 offset. Extend its limit by
                # that offset, rather than clipping the reproduced motion.
                lower -= 0.087
                upper -= 0.087
                bound.set("lower", str(lower))
                bound.set("upper", str(upper))
            limits[name] = {
                "lower": lower, "upper": upper,
                "velocity": min(float(bound.get("velocity", 0.6)), 0.6),
            }
            damping = joint.find("dynamics")
            if damping is not None:
                damping.set("damping", "0.1")
        elif joint.get("type") != "fixed":
            # Ideal planar model motion replaces wheel/caster dynamics;
            # gripper spring joints are held at zero for reproducibility.
            joint.set("type", "fixed")
            for child in list(joint):
                if child.tag in ("axis", "limit", "dynamics", "mimic"):
                    joint.remove(child)
    preserve = sub(robot, "gazebo", reference="hand_palm_joint")
    sub(preserve, "preserveFixedJoint", "true")
    save_xml(assets / "robot.urdf", robot)
    convert = subprocess.run(["gz", "sdf", "-p", str(assets / "robot.urdf")],
                             text=True, capture_output=True, check=True, timeout=90)
    sdf = ET.fromstring(convert.stdout)
    model = sdf.find("model")
    if model is None or model.find("link[@name='hand_palm_link']") is None:
        raise RuntimeError("Converted HSR model must retain hand_palm_link.")
    model.set("name", "hsr")
    canonical_link = "base_footprint"
    model.set("canonical_link", canonical_link)
    sub(model, "self_collide", "false")
    for link in model.findall("link"):
        for sensor in list(link.findall("sensor")):
            link.remove(sensor)
        if link.get("name") in ("torso_lift_link", "head_pan_link", "head_tilt_link"):
            # With Harmonic/DART, this branch stalls against gravity when
            # driven by the ideal velocity-based position controller. Keep
            # its kinematics and mass but omit gravity in this learning demo.
            # The isolated torso command check reaches 0.11 m with this
            # setting; increasing velocity alone does not resolve the stall.
            for gravity in list(link.findall("gravity")):
                link.remove(gravity)
            sub(link, "gravity", "false")
        for collision in link.findall("collision"):
            surface = collision.find("surface")
            if surface is None:
                surface = sub(collision, "surface")
            contact = surface.find("contact")
            if contact is None:
                contact = sub(surface, "contact")
            sub(contact, "collide_bitmask", 1)
    home = expand_hand(HOME)
    for name, bound in sorted(limits.items()):
        plugin(model, "gz-sim-joint-position-controller-system",
               "gz::sim::systems::JointPositionController",
               joint_name=name, topic=f"/household/joint/{name}/cmd",
               use_velocity_commands="true", cmd_max=bound["velocity"],
               initial_position=0.0)
    plugin(model, "gz-sim-joint-state-publisher-system",
           "gz::sim::systems::JointStatePublisher", topic="/model/hsr/joint_state")
    plugin(model, "gz-sim-velocity-control-system",
           "gz::sim::systems::VelocityControl", topic="/household/cmd_vel")
    plugin(model, "gz-sim-pose-publisher-system", "gz::sim::systems::PosePublisher",
           publish_link_pose="true", publish_model_pose="true",
           publish_nested_model_pose="true", use_pose_vector_msg="true",
           update_frequency=30)
    # Poses below are derived from the actual vendor URDF, not guessed.
    pick = expand_hand(PICK)
    pickup_matrix = fk(robot, pick)
    grasp_position = point(pickup_matrix, GRASP_OFFSET)
    spawn_z = 0.003
    grasp_position[2] += spawn_z
    tabletop = grasp_position[2] - 0.03
    objects = {
        "cube": {"position": list(grasp_position), "shape": "box", "size": [0.06]*3,
                 "color": "0.95 0.18 0.12 1", "mass": 0.08},
        "ball": {"position": [grasp_position[0], grasp_position[1]-0.26, grasp_position[2]],
                 "shape": "sphere", "radius": 0.03, "color": "0.1 0.4 0.95 1", "mass": 0.04},
    }
    for name in objects:
        plugin(model, "gz-sim-detachable-joint-system", "gz::sim::systems::DetachableJoint",
               parent_link="hand_palm_link", child_model=name, child_link="link",
               attach_topic=f"/household/grasp/{name}/attach",
               detach_topic=f"/household/grasp/{name}/detach",
               output_topic=f"/household/grasp/{name}/state")
    save_xml(assets / "model.sdf", sdf)

    world_sdf = ET.Element("sdf", version="1.10")
    world = sub(world_sdf, "world", name="household")
    sub(world, "gravity", "0 0 -9.81")
    physics = sub(world, "physics", name="household_physics", type="ignored")
    sub(physics, "max_step_size", 0.001)
    sub(physics, "real_time_factor", 1.0)
    plugin(world, "gz-sim-physics-system", "gz::sim::systems::Physics")
    plugin(world, "gz-sim-user-commands-system", "gz::sim::systems::UserCommands")
    plugin(world, "gz-sim-scene-broadcaster-system", "gz::sim::systems::SceneBroadcaster")
    scene = sub(world, "scene")
    sub(scene, "ambient", "0.65 0.65 0.65 1")
    sub(scene, "background", "0.9 0.93 0.96 1")
    light = sub(world, "light", type="directional", name="sun")
    sub(light, "pose", "0 0 8 0 0 0")
    sub(light, "cast_shadows", "true")
    sub(light, "diffuse", "0.85 0.85 0.85 1")
    sub(light, "specular", "0.2 0.2 0.2 1")
    sub(light, "direction", "-0.5 -0.3 -1")
    attenuation = sub(light, "attenuation")
    sub(attenuation, "range", 1000)
    sub(attenuation, "constant", 0.9)
    sub(attenuation, "linear", 0.01)
    sub(attenuation, "quadratic", 0.001)
    floor = sub(world, "model", name="floor")
    sub(floor, "static", "true")
    shape_link(floor, "floor", [0, 0, -0.025, 0, 0, 0], [6, 6, 0.05],
               color="0.86 0.84 0.79 1")
    table = sub(world, "model", name="table")
    sub(table, "static", "true")
    table_x, table_y = grasp_position[0]+0.09, grasp_position[1]-0.13
    shape_link(table, "top", [table_x, table_y, tabletop-0.025, 0, 0, 0],
               [0.55, 0.68, 0.05], color="0.58 0.37 0.2 1")
    for i, dx in enumerate((-0.225, 0.225)):
        for j, dy in enumerate((-0.29, 0.29)):
            shape_link(table, f"leg_{i}_{j}", [table_x+dx, table_y+dy, (tabletop-0.05)/2, 0, 0, 0],
                       [0.04, 0.04, tabletop-0.05], color="0.32 0.29 0.27 1")
    tray = sub(world, "model", name="tray")
    sub(tray, "static", "true")
    tray_position = [grasp_position[0]+0.10, grasp_position[1]+0.20, tabletop+0.005]
    shape_link(tray, "tray", tray_position+[0, 0, 0], [0.22, 0.14, 0.01],
               color="0.1 0.6 0.42 1")
    for name, spec in objects.items():
        obj = sub(world, "model", name=name)
        sub(obj, "pose", " ".join(map(str, spec["position"]+[0, 0, 0])))
        shape_link(obj, "link", [0]*6, spec.get("size"), spec.get("radius"),
                   spec["color"], spec["mass"], collision_mask=2)
        plugin(obj, "gz-sim-pose-publisher-system", "gz::sim::systems::PosePublisher",
               publish_link_pose="false", publish_model_pose="true",
               publish_nested_model_pose="false", use_pose_vector_msg="true",
               update_frequency=30)
    robot_instance = copy.deepcopy(model)
    sub(robot_instance, "pose", f"0 0 {spawn_z} 0 0 0")
    world.append(robot_instance)
    save_xml(assets / "world.sdf", world_sdf)
    (assets / "bridge.yaml").write_text(json.dumps(make_bridge(limits, objects), indent=2)+"\n")
    try:
        revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                           text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        revision = "unknown"
    manifest = {
        "world": "household", "base_model": "hsr", "base_link": canonical_link,
        "base_spawn_position": [0, 0, spawn_z],
        "controlled_joints": limits, "home": home, "pick_pose": pick,
        "mimic_relations": {k: v for k, v in mimic.items() if k in controlled},
        "grasp_link": "hand_palm_link", "grasp_offset": GRASP_OFFSET,
        "grasp_open": 0.7, "grasp_closed": 0.4,
        "pickup_palm_transform_relative_base": pickup_matrix,
        "pickup_grasp_position_world": grasp_position,
        "objects": objects, "tabletop_height": tabletop,
        "tray_position": tray_position,
        "initial_attachment_state": "attached",
        "startup_requirement": "Detach cube and ball after both Gazebo plugins initialize, before commanding any robot motion.",
        "simplifications": [
            "Official HSR-B visual meshes, kinematic chain, collision shapes and joint limits.",
            "Ideal Gazebo joint velocity control; no Toyota hardware or whole-body controllers.",
            "Torso/head branch gravity disabled for stable ideal-controller motion in Harmonic/DART.",
            "Planar model velocity control; wheel/caster mechanics fixed, not full omnidirectional base dynamics.",
            "Finger spring joints fixed at zero; mimic joints commanded explicitly.",
            "Assisted object holding via a detachable fixed joint; not friction-based grasping.",
            "Robot-object contact disabled, while object-table/floor contact stays enabled.",
            "No camera, perception or automatic obstacle avoidance in this initial scene.",
        ],
        "provenance": {
            "repository": "https://github.com/hsr-project/hsrb_common",
            "branch": "jazzy", "commit": revision, "license": "BSD-3-Clause-Clear",
            "source_model": "hsrb_description/robots/hsrb4s.urdf.xacro",
            "copyright": "Toyota Motor Corporation",
            "license_file": "../vendor/hsrb_common/LICENSE.txt",
        },
    }
    (assets / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    shutil.copyfile(repo / "LICENSE.txt", assets / "HSR_LICENSE.txt")
    print(json.dumps({"assets": str(assets), "joints": len(limits), "objects": objects,
                      "tabletop_height": tabletop, "pickup": grasp_position}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    build(parser.parse_args().root)
