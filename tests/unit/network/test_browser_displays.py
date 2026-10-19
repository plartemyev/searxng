# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for X display lifecycle hygiene: stale lock/socket cleanup,
Xvfb startup verification and the self-heal of a wedged display."""

# pylint: disable=missing-module-docstring,missing-function-docstring
# pylint: disable=protected-access,unused-argument

import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

from searx.network import browser

# a display number nothing in a sane environment serves
DISPLAY = 9231
LOCK = f"/tmp/.X{DISPLAY}-lock"  # noqa: S108
SOCKET = f"/tmp/.X11-unix/X{DISPLAY}"  # noqa: S108

SLEEPER = "import time; time.sleep(30)"


def _write_leftovers(owner_pid: int) -> None:
    """Plant the artifacts a SIGKILLed X server leaves behind."""
    os.makedirs("/tmp/.X11-unix", exist_ok=True)
    with open(LOCK, "w", encoding="ascii") as lock_file:
        lock_file.write("%10d" % owner_pid)
    with open(SOCKET, "wb"):
        pass
    old = time.time() - 86400
    os.utime(SOCKET, (old, old))


def _remove_leftovers() -> None:
    for path in (LOCK, SOCKET):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


@pytest.fixture
def leftovers():
    _remove_leftovers()
    yield
    _remove_leftovers()


def _spawn(argv):
    return subprocess.Popen(  # pylint: disable=consider-using-with
        [sys.executable, "-c", SLEEPER, *argv]
    )


# ----------------------------------------------------------------------
# _argv_names_display


def test_argv_names_display_exact():
    assert browser._argv_names_display(["/usr/sbin/Xvfb", ":104", "-screen"], 104)


def test_argv_names_display_with_screen_suffix():
    assert browser._argv_names_display(["Xorg", ":9231.0"], 9231)


def test_argv_display_number_is_not_matched_as_substring():
    # ":10" must not count as display :104 (the old substring check did)
    assert not browser._argv_names_display(["/usr/sbin/Xvfb", ":10"], 104)


def test_argv_unrelated_display_does_not_match():
    assert not browser._argv_names_display(["/usr/sbin/Xvfb", ":99"], 104)


# ----------------------------------------------------------------------
# _cleanup_stale_x_locks


def test_cleanup_keeps_lock_of_live_server_for_this_display(leftovers):
    owner = _spawn([f":{DISPLAY}"])
    try:
        _write_leftovers(owner.pid)
        browser._cleanup_stale_x_locks(DISPLAY)
        assert os.path.exists(LOCK)
        assert os.path.exists(SOCKET)
    finally:
        owner.terminate()
        owner.wait()


def test_cleanup_removes_lock_whose_pid_was_reused(leftovers):
    # the production wedge: after a container restart the stale lock's PID
    # belongs to a live process that is NOT an X server for this display
    owner = _spawn([])
    try:
        _write_leftovers(owner.pid)
        browser._cleanup_stale_x_locks(DISPLAY)
        assert not os.path.exists(LOCK)
        assert not os.path.exists(SOCKET)
    finally:
        owner.terminate()
        owner.wait()


def test_cleanup_removes_lock_of_dead_owner(leftovers):
    owner = _spawn([])
    owner.terminate()
    owner.wait()
    _write_leftovers(owner.pid)
    browser._cleanup_stale_x_locks(DISPLAY)
    assert not os.path.exists(LOCK)
    assert not os.path.exists(SOCKET)


def test_cleanup_removes_orphaned_socket_without_lock(leftovers):
    os.makedirs("/tmp/.X11-unix", exist_ok=True)
    with open(SOCKET, "wb"):
        pass
    browser._cleanup_stale_x_locks(DISPLAY)
    assert not os.path.exists(SOCKET)


# ----------------------------------------------------------------------
# _xvfb_came_up


class _FakeProcess:
    def __init__(self, alive=True):
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


def _listening_socket(path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(path)
    sock.listen(1)
    return sock


def test_xvfb_came_up_rejects_leftover_socket(leftovers, monkeypatch):
    # a socket file from a dead server must not count, however it got there
    _write_leftovers(1)
    before = os.stat(SOCKET).st_mtime
    assert not browser._xvfb_came_up(_FakeProcess(), SOCKET, before)


def test_xvfb_came_up_rejects_dead_process(leftovers):
    sock = _listening_socket(SOCKET)
    try:
        old = time.time() - 86400
        os.utime(SOCKET, (old, old))
        assert not browser._xvfb_came_up(_FakeProcess(alive=False), SOCKET, old)
    finally:
        sock.close()
        os.remove(SOCKET)


def test_xvfb_came_up_accepts_fresh_listening_socket(leftovers):
    sock = _listening_socket(SOCKET)
    try:
        assert browser._xvfb_came_up(_FakeProcess(), SOCKET, time.time() - 60)
    finally:
        sock.close()
        os.remove(SOCKET)


def test_xvfb_came_up_rejects_missing_socket(leftovers):
    assert not browser._xvfb_came_up(_FakeProcess(), SOCKET, 0.0)


# ----------------------------------------------------------------------
# the production wedge, end to end (needs a real Xvfb)


@pytest.mark.skipif(shutil.which("Xvfb") is None, reason="Xvfb not installed")
def test_ensure_display_self_heals_wedged_display(leftovers, monkeypatch):
    """A display wedged exactly like the 2026-10 outage (SIGKILLed Xvfb,
    leftover lock + stale socket, lock PID reused by a live unrelated
    process) must come back without operator action."""
    os.makedirs("/tmp/.X11-unix", exist_ok=True)
    wedge_display = 99 + DISPLAY  # keep the pool's own :99-:104 out of it
    with open(f"/tmp/.X{wedge_display}-lock", "w", encoding="ascii") as lock_file:  # noqa: S108
        lock_file.write("%10d" % os.getpid())  # live, but not an X server
    stale_socket = f"/tmp/.X11-unix/X{wedge_display}"  # noqa: S108
    with open(stale_socket, "wb"):
        pass

    lane_index = DISPLAY  # :9231 + 99 -> the wedged display number
    try:
        display = browser.ensure_display(lane_index)
        assert display == f":{wedge_display}"
        process = browser._xvfb_processes[display]
        assert process.poll() is None
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(stale_socket)  # a real X server now listens
        sock.close()
    finally:
        process = browser._xvfb_processes.pop(f":{wedge_display}", None)
        if process is not None:
            process.terminate()
            process.wait()
        browser._cleanup_stale_x_locks(wedge_display)
