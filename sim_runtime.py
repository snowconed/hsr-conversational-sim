"""Start and stop only this application's Gazebo and ROS bridge processes."""
import os
from pathlib import Path
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parent


def configure_environment():
    os.environ.setdefault("ROS_DOMAIN_ID", "84")
    os.environ.setdefault("ROS_AUTOMATIC_DISCOVERY_RANGE", "LOCALHOST")
    os.environ.setdefault("GZ_PARTITION", "hsr_household_local")
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")


class Simulation:
    def __init__(self, gui=False):
        configure_environment()
        self.processes = []
        self.logs = []
        (ROOT / "logs").mkdir(exist_ok=True)
        try:
            self.start(
                ["gz", "sim", "-r", "-s", "-v2", str(ROOT / "assets/world.sdf")],
                "simulation",
            )
            self.start(
                ["ros2", "run", "ros_gz_bridge", "parameter_bridge",
                 "--ros-args", "-p", f"config_file:={ROOT / 'assets/bridge.yaml'}"],
                "bridge",
            )
            if gui:
                self.start(["gz", "sim", "-g", "-v2"], "gazebo_gui")
        except BaseException:
            self.close()
            raise

    def start(self, args, name):
        logfile = (ROOT / "logs" / f"{name}.log").open("w", encoding="utf-8")
        self.logs.append(logfile)
        process = subprocess.Popen(
            args, cwd=ROOT, stdout=logfile, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.processes.append(process)
        return process

    def check(self):
        for process in self.processes:
            if process.poll() is not None:
                raise RuntimeError(
                    f"Simulation component exited ({process.returncode}); see logs/."
                )

    def close(self):
        for sig, timeout in [(signal.SIGINT, 8), (signal.SIGTERM, 3), (signal.SIGKILL, 2)]:
            remaining = [p for p in self.processes if p.poll() is None]
            for process in remaining:
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + timeout
            for process in remaining:
                try:
                    process.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
        for logfile in self.logs:
            logfile.close()
        self.logs.clear()
