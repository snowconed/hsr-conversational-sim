#!/usr/bin/env bash
set -eo pipefail
HOUSEHOLD_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ ! -f /opt/ros/jazzy/setup.bash ]]; then
    echo "Install ROS 2 Jazzy and Gazebo Harmonic in Ubuntu 24.04 first." >&2
    exit 1
fi
source /opt/ros/jazzy/setup.bash
for tool in git python3 xacro gz; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Missing prerequisite: $tool (ROS 2 Jazzy, ros-jazzy-ros-gz, ros-jazzy-xacro, git, python3-venv)." >&2
        exit 1
    fi
done
# Fetch the public model at the revision used by this demo. Generated assets
# must be built locally because their mesh paths depend on the project folder.
MODEL_REPOSITORY="https://github.com/hsr-project/hsrb_common.git"
MODEL_REVISION="c0b8ad5f813f1066b718f24911397d2a7f995cad"
MODEL_DIRECTORY="$HOUSEHOLD_DIR/vendor/hsrb_common"
if [[ ! -e "$MODEL_DIRECTORY" ]]; then
    mkdir -p "$HOUSEHOLD_DIR/vendor"
    git clone --branch jazzy --single-branch "$MODEL_REPOSITORY" "$MODEL_DIRECTORY"
    git -C "$MODEL_DIRECTORY" checkout --detach "$MODEL_REVISION"
elif [[ "$(git -C "$MODEL_DIRECTORY" rev-parse HEAD)" != "$MODEL_REVISION" ]]; then
    echo "The existing vendor/hsrb_common revision differs from this demo. Keep a copy and use the expected model revision: $MODEL_REVISION" >&2
    exit 1
fi
python3 -m venv --system-site-packages "$HOUSEHOLD_DIR/.venv"
"$HOUSEHOLD_DIR/.venv/bin/python" -m pip install -r "$HOUSEHOLD_DIR/requirements.txt"
"$HOUSEHOLD_DIR/.venv/bin/python" "$HOUSEHOLD_DIR/build_model.py"
