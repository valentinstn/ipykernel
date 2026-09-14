import os
import subprocess
import sys
import time
from textwrap import dedent

import pytest
from jupyter_client.blocking.client import BlockingKernelClient

from .test_embed_kernel import setup_kernel

TIMEOUT = 15

if os.name == "nt":
    pytest.skip("skipping tests on windows", allow_module_level=True)


@pytest.mark.flaky(max_runs=3)
def test_ipython_start_kernel_userns():
    import IPython

    if IPython.version_info > (9, 0):  # noqa:SIM108
        EXPECTED = "IPythonMainModule"
    else:
        # not this since https://github.com/ipython/ipython/pull/14754
        EXPECTED = "DummyMod"

    cmd = dedent(
        """
        from ipykernel.kernelapp import launch_new_instance
        ns = {"custom": 123}
        launch_new_instance(user_ns=ns)
        """
    )

    with setup_kernel(cmd) as client:
        client.inspect("custom")
        msg = client.get_shell_msg(timeout=TIMEOUT)
        content = msg["content"]
        assert content["found"]
        text = content["data"]["text/plain"]
        assert "123" in text

        # user_module should be an instance of DummyMod
        client.execute("usermod = get_ipython().user_module")
        msg = client.get_shell_msg(timeout=TIMEOUT)
        content = msg["content"]
        assert content["status"] == "ok"
        client.inspect("usermod")
        msg = client.get_shell_msg(timeout=TIMEOUT)
        content = msg["content"]
        assert content["found"]
        text = content["data"]["text/plain"]
        assert EXPECTED in text


def test_start_kernel_background_thread():
    cmd = dedent(
        """
        import threading
        import asyncio
        from ipykernel.kernelapp import launch_new_instance

        def launch():
            # Threads don't always have a default event loop so we need to
            # create and set a default
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            launch_new_instance()

        thread = threading.Thread(target=launch)
        thread.start()
        thread.join()
        """
    )

    with setup_kernel(cmd) as client:
        client.execute("a = 1")
        msg = client.get_shell_msg(timeout=TIMEOUT)
        content = msg["content"]
        assert content["status"] == "ok"

        client.inspect("a")
        msg = client.get_shell_msg(timeout=TIMEOUT)
        content = msg["content"]
        assert content["found"]
        text = content["data"]["text/plain"]
        assert "1" in text


def test_profile_startup_import_does_not_deadlock(tmp_path):
    """Profile imports must finish before the channel threads are started."""
    profile = tmp_path / "profile"
    startup = profile / "startup"
    startup.mkdir(parents=True)
    (tmp_path / "repro_target.py").write_text(
        dedent(
            """
            import builtins
            import threading

            if any(thread.name in {"Control", "Shell channel"} for thread in threading.enumerate()):
                builtins._startup_import_event.wait()
            """
        )
    )
    (startup / "00-import-order.py").write_text(
        dedent(
            f"""
            import builtins
            import sys
            import threading

            from ipykernel.kernelapp import IPKernelApp

            builtins._startup_import_event = threading.Event()
            app = IPKernelApp.instance()
            sys.path.insert(0, {str(tmp_path)!r})

            def import_on_channel_thread():
                __import__("repro_target")
                builtins._startup_import_event.set()

            app.control_thread.io_loop.add_callback(import_on_channel_thread)
            __import__("repro_target")
            """
        )
    )
    connection_file = tmp_path / "connection.json"
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
        cwd=tmp_path,
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
            pytest.fail("Kernel did not create a connection file")

        client = BlockingKernelClient(connection_file=str(connection_file))
        client.load_connection_file()
        client.start_channels()
        client.wait_for_ready(timeout=5)
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


@pytest.mark.flaky(max_runs=3)
def test_ipython_start_kernel_no_userns():
    # Issue #4188 - user_ns should be passed to shell as None, not {}
    cmd = dedent(
        """
        from ipykernel.kernelapp import launch_new_instance
        launch_new_instance()
        """
    )

    with setup_kernel(cmd) as client:
        # user_module should not be an instance of DummyMod
        client.execute("usermod = get_ipython().user_module")
        msg = client.get_shell_msg(timeout=TIMEOUT)
        content = msg["content"]
        assert content["status"] == "ok"
        client.inspect("usermod")
        msg = client.get_shell_msg(timeout=TIMEOUT)
        content = msg["content"]
        assert content["found"]
        text = content["data"]["text/plain"]
        assert "DummyMod" not in text
