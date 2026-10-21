#!/usr/bin/env bash
set -eo pipefail
source /opt/ros/jazzy/setup.bash
HOUSEHOLD_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$HOUSEHOLD_DIR/.venv/bin/python" "$HOUSEHOLD_DIR/run_household.py" "$@"
