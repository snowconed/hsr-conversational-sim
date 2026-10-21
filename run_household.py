"""Local application launcher. GUI is opened only by explicit user execution."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

from sim_runtime import ROOT, Simulation, configure_environment


UI_URL = "http://127.0.0.1:8765"


def wait_for_web_server(simulation, timeout=180.0):
    """Advertise the UI only after this application's HTTP API is serving."""
    beginning = time.monotonic()
    deadline = beginning + timeout
    next_notice = beginning + 10
    while time.monotonic() < deadline:
        simulation.check()
        try:
            with urlopen(
                f"{UI_URL}/api/state",
                timeout=min(1.0, max(0.01, deadline - time.monotonic())),
            ) as response:
                if response.status == 200:
                    state = json.loads(response.read(1024 * 1024))
                    if (
                        isinstance(state, dict)
                        and type(state.get("command_epoch")) is int
                        and type(state.get("ready")) is bool
                        and isinstance(state.get("program"), dict)
                        and isinstance(state.get("api"), dict)
                        and type(state["api"].get("configured")) is bool
                    ):
                        # A robot still initializing may have ready=False.
                        # The UI can show that progress once HTTP is available.
                        simulation.check()
                        return time.monotonic() - beginning
        except (URLError, OSError, ValueError):
            pass
        if time.monotonic() >= next_notice:
            elapsed = time.monotonic() - beginning
            print(f"操作画面の準備を続けています（{elapsed:.0f} 秒経過）。", flush=True)
            next_notice = time.monotonic() + 10
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.5, remaining))
    raise RuntimeError(f"操作画面の準備が {timeout:g} 秒以内に完了しませんでした。")


def web_server_log_tail():
    path = ROOT / "logs" / "web_server.log"
    try:
        # Only report the final diagnostic lines, with credentials removed.
        with path.open("rb") as source:
            source.seek(0, os.SEEK_END)
            source.seek(max(0, source.tell() - 8192))
            tail = source.read().decode("utf-8", errors="replace")
        tail = "\n".join(tail.splitlines()[-25:]).strip()
    except OSError:
        return f"診断ログを読み取れませんでした: {path}"
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        tail = tail.replace(api_key, "[redacted]")
    tail = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", tail)
    tail = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", tail)
    return f"診断ログ: {path}\n{tail or '(ログはまだありません)'}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    configure_environment()
    lockfile = open(f"/tmp/hsr-household-{os.getuid()}.lock", "w")
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("HSR household simulation is already running.", flush=True)
        return 1

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    simulation = None
    started = False
    try:
        if not (ROOT / "assets/world.sdf").exists():
            raise RuntimeError("Run setup_environment.sh before starting.")
        print("ロボットと操作画面を準備しています。起動完了までお待ちください。", flush=True)
        simulation = Simulation(gui=not args.headless)
        simulation.start(
            [sys.executable, "-m", "uvicorn", "web_server:app",
             "--host", "127.0.0.1", "--port", "8765"],
            "web_server",
        )
        elapsed = wait_for_web_server(simulation)
        started = True
        print(f"起動しました（{elapsed:.1f} 秒）。操作画面: {UI_URL}", flush=True)
        print("終了するにはこのターミナルで Ctrl+C を押してください。", flush=True)
        while True:
            simulation.check()
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    except Exception as error:
        label = "アプリが停止しました" if started else "起動できませんでした"
        print(f"{label}: {error}", file=sys.stderr, flush=True)
        print(web_server_log_tail(), file=sys.stderr, flush=True)
        return 1
    finally:
        if simulation:
            simulation.close()
        lockfile.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
