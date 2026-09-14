"""Reproduce the kernel-startup import deadlock.

Run this file with the checkout's Python environment::

    .venv/bin/python examples/reproduce_startup_import_deadlock.py

On an unpatched checkout, the kernel writes its connection file but never
answers ``kernel_info``.  The profile startup file and imported module below
are the complete reproducer; no Savvy modules are required.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

from jupyter_client.blocking.client import BlockingKernelClient

STARTUP = """
import builtins
import sys
import threading

from ipykernel.kernelapp import IPKernelApp


builtins._startup_import_event = threading.Event()
app = IPKernelApp.instance()
sys.path.insert(0, {root!r})


def import_on_channel_thread():
    __import__("repro_target")
    builtins._startup_import_event.set()


# The old startup order has already started this thread.  The fixed order
# queues this callback until after profile startup has finished.
app.control_thread.io_loop.add_callback(import_on_channel_thread)
__import__("repro_target")
"""

TARGET = """
import builtins
import threading


# This module holds its import lock while an already-running kernel channel
# thread tries to import it too.  The main thread waits for that callback,
# while the callback waits for this import lock: a minimal two-thread import
# deadlock.
if any(thread.name in {"Control", "Shell channel"} for thread in threading.enumerate()):
    builtins._startup_import_event.wait()
"""


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ipykernel-import-deadlock-") as directory:
        root = Path(directory)
        profile = root / "profile"
        (profile / "startup").mkdir(parents=True)
        (profile / "startup" / "00-reproduce.py").write_text(STARTUP.format(root=str(root)))
        (root / "repro_target.py").write_text(TARGET)
        connection_file = root / "connection.json"

        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ipykernel_launcher",
                "--profile-dir",
                str(profile),
                "-f",
                str(connection_file),
            ],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        client = None
        try:
            deadline = time.monotonic() + 5
            while not connection_file.exists() and process.poll() is None:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)

            if not connection_file.exists():
                print("Kernel did not create a connection file.")
                return 1

            client = BlockingKernelClient(connection_file=str(connection_file))
            client.load_connection_file()
            client.start_channels()
            try:
                client.wait_for_ready(timeout=3)
            except Exception as error:
                print("REPRODUCED: kernel_info did not arrive (the kernel is stuck).")
                print(f"{type(error).__name__}: {error}")
                return 0
            else:
                print("Kernel became ready; the startup-order fix is present.")
                return 0
        finally:
            if client is not None:
                client.stop_channels()
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if process.stdout:
                process.stdout.close()


if __name__ == "__main__":
    raise SystemExit(main())
