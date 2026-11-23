"""bounded_run: the bar's finite helpers end as a whole process group (exit, deadline, stop),
and BarWidget.qml starts every finite helper through it."""
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bounded_run  # noqa: E402

SUPERVISOR = str(ROOT / "bounded_run.py")

# A helper that leaves a child behind in its group: the child writes its pid, then sleeps.
# term_ignored: the child ignores SIGTERM, so only the SIGKILL after the grace ends it.
LEAVE_CHILD = r"""
import os, signal, subprocess, sys, time
pidfile, mode = sys.argv[1], sys.argv[2]
code = "import os,signal,sys,time\n"
if mode == "term_ignored":
    code += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
code += "open(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(300)\n"
subprocess.Popen([sys.executable, "-c", code, pidfile], stdin=subprocess.DEVNULL,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while not os.path.exists(pidfile) or not open(pidfile).read():
    time.sleep(0.02)
if sys.argv[3] == "exit":
    sys.exit(3)
time.sleep(300)
"""


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
    except OSError:
        return False
    return data[data.rfind(b")") + 2:].split()[0] not in (b"Z", b"X")


class BoundedRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pidfile = os.path.join(self.tmp.name, "child.pid")
        self.leftover = None
        self.addCleanup(self.kill_leftover)

    def kill_leftover(self):
        try:
            pid = int(Path(self.pidfile).read_text())
        except (OSError, ValueError):
            return
        if alive(pid):
            os.kill(pid, signal.SIGKILL)

    def start(self, seconds, mode, end, grace=None):
        argv = [sys.executable, "-I", SUPERVISOR, str(seconds), "--",
                sys.executable, "-c", LEAVE_CHILD, self.pidfile, mode, end]
        env = dict(os.environ)
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=env)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def child_pid(self, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                text = Path(self.pidfile).read_text()
                if text:
                    return int(text)
            except OSError:
                pass
            time.sleep(0.02)
        self.fail("the helper's child never started")

    def wait_gone(self, pid, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not alive(pid):
                return True
            time.sleep(0.05)
        return False

    def test_a_finished_helper_takes_what_it_left_behind_with_it(self):
        proc = self.start(60, "plain", "exit")
        child = self.child_pid()
        self.assertEqual(proc.wait(timeout=15), 3)           # the helper's own status
        self.assertTrue(self.wait_gone(child, 2))

    def test_the_deadline_ends_the_whole_group(self):
        proc = self.start(1, "plain", "stay")
        child = self.child_pid()
        self.assertEqual(proc.wait(timeout=15), 124)
        self.assertTrue(self.wait_gone(child, 2))

    def test_sigterm_to_the_supervisor_ends_the_group_with_sigkill_after_the_grace(self):
        # What the bar's outer deadline does: Process running = false sends SIGTERM to the
        # supervisor only. A member that ignores SIGTERM still ends (SIGKILL after the grace).
        proc = self.start(60, "term_ignored", "stay")
        child = self.child_pid()
        time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=bounded_run.GRACE + 10), 143)
        self.assertTrue(self.wait_gone(child, 2))

    def test_the_helper_runs_in_a_group_of_its_own(self):
        proc = self.start(60, "plain", "stay")
        child = self.child_pid()
        self.assertNotEqual(os.getpgid(child), os.getpgid(0))
        self.assertNotEqual(os.getpgid(child), proc.pid)       # not the supervisor's group
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)

    def test_something_started_in_its_own_session_is_left_alone(self):
        # Apps (uwsm-app, gio launch), notifications and `omarchy theme set` start like this.
        code = ("import subprocess,sys;"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(300)'],"
                "start_new_session=True);open(sys.argv[1],'w').write(str(p.pid))")
        proc = subprocess.run([sys.executable, "-I", SUPERVISOR, "30", "--",
                               sys.executable, "-c", code, self.pidfile], timeout=20)
        self.assertEqual(proc.returncode, 0)
        pid = self.child_pid()
        self.assertTrue(alive(pid))

    def test_group_signal_reaches_members_after_the_leader_was_reaped(self):
        # The case of a supervisor that died: the leader exits and init reaps it, its child
        # (ignoring SIGTERM) stays. The pidfd group signal still reaches that child.
        code = ("import subprocess,sys;p=subprocess.Popen([sys.executable,'-c',"
                "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)']);"
                "print(p.pid,flush=True)")
        leader = subprocess.Popen([sys.executable, "-c", code], start_new_session=True,
                                  stdout=subprocess.PIPE, text=True)
        pidfd = os.pidfd_open(leader.pid)
        self.addCleanup(os.close, pidfd)
        child = int(leader.stdout.readline())
        leader.stdout.close()
        leader.wait()                                   # leader reaped
        self.addCleanup(lambda: alive(child) and os.kill(child, signal.SIGKILL))
        self.assertTrue(bounded_run._group_signal(pidfd, leader.pid, signal.SIGKILL))
        self.assertTrue(self.wait_gone(child, 3))
        self.assertFalse(bounded_run._group_signal(pidfd, leader.pid, signal.SIGTERM))  # empty

    def test_usage_errors_start_nothing(self):
        for args in ([], ["5"], ["5", "x", "true"], ["0", "--", "true"], ["nan", "--", "true"],
                     ["99999", "--", "true"], ["abc", "--", "true"]):
            self.assertEqual(bounded_run.main(args), 125, args)

    def test_a_program_that_cannot_start(self):
        result = subprocess.run([sys.executable, "-I", SUPERVISOR, "5", "--",
                                 os.path.join(self.tmp.name, "missing")], timeout=10)
        self.assertEqual(result.returncode, 125)

    def test_no_watchdog_no_program(self):
        marker = os.path.join(self.tmp.name, "ran")
        original = bounded_run._start_watchdog
        bounded_run._start_watchdog = lambda grace: None
        try:
            code = bounded_run.run(5, ["/usr/bin/touch", marker])
        finally:
            bounded_run._start_watchdog = original
        self.assertEqual(code, 125)
        self.assertFalse(os.path.exists(marker))

    def test_a_watchdog_that_cannot_be_told_means_no_start_and_no_start_line(self):
        import contextlib, io
        marker = os.path.join(self.tmp.name, "ran")
        original = bounded_run._child_setup

        def failing(parent, sock):
            def setup():
                raise bounded_run._NotStarted("watchdog not told")
            return setup
        bounded_run._child_setup = failing
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = bounded_run.run(5, ["/usr/bin/touch", marker], report_start=True)
        finally:
            bounded_run._child_setup = original
        self.assertEqual(code, 125)
        self.assertFalse(os.path.exists(marker))
        self.assertNotIn(bounded_run.STARTED_LINE, out.getvalue())

    def test_the_start_line_comes_only_once_the_program_really_started(self):
        missing = subprocess.run([sys.executable, "-I", SUPERVISOR, "--report-start", "5", "--",
                                  os.path.join(self.tmp.name, "missing")],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(missing.returncode, 125)
        self.assertNotIn(bounded_run.STARTED_LINE, missing.stdout)
        ok = subprocess.run([sys.executable, "-I", SUPERVISOR, "--report-start", "5", "--", "true"],
                            capture_output=True, text=True, timeout=10)
        self.assertEqual(ok.returncode, 0)
        self.assertEqual(ok.stdout.splitlines()[0], bounded_run.STARTED_LINE)

    def test_sigkill_to_the_supervisor_still_ends_the_group(self):
        # Quickshell kills a running Process when the shell exits: the watchdog ends the group.
        proc = self.start(60, "term_ignored", "exit")    # leader exits 3, child stays (ignores TERM)
        child = self.child_pid()
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        self.assertTrue(self.wait_gone(child, bounded_run.GRACE + 5))

    def test_sigkill_to_the_supervisor_while_the_leader_runs(self):
        proc = self.start(60, "plain", "stay")
        child = self.child_pid()
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        self.assertTrue(self.wait_gone(child, bounded_run.GRACE + 5))


    def test_sigkill_to_the_supervisor_during_its_own_cleanup_after_the_leader_exited(self):
        # The window of the 2.6.9 review: the leader exited, the supervisor is ending the group
        # (its child ignores SIGTERM, so the supervisor is inside the grace) and is killed there.
        # init then reaps the leader; the watchdog must still end the child.
        proc = self.start(60, "term_ignored", "exit")
        child = self.child_pid()
        end = time.monotonic() + 10
        while time.monotonic() < end and alive(os.getpgid(child)):
            time.sleep(0.02)                                 # the leader has exited
        time.sleep(0.5)                                      # the supervisor is in its grace now
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        self.assertTrue(self.wait_gone(child, bounded_run.GRACE + 5))

    def test_sigkill_to_the_supervisor_during_the_grace_of_a_stop(self):
        proc = self.start(60, "term_ignored", "stay")
        child = self.child_pid()
        time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)                     # the supervisor starts ending the group
        time.sleep(0.5)
        proc.send_signal(signal.SIGKILL)                     # ... and dies in the middle of it
        proc.wait(timeout=10)
        self.assertTrue(self.wait_gone(child, bounded_run.GRACE + 5))

    def test_sigkill_to_the_supervisor_right_after_the_start(self):
        code = ("import subprocess,sys;"
                "subprocess.Popen([sys.executable,'-c','import os,sys,time;"
                "open(sys.argv[1],\'w\').write(str(os.getpid()));time.sleep(300)',sys.argv[1]])")
        proc = subprocess.Popen([sys.executable, "-I", SUPERVISOR, "--report-start", "60", "--",
                                 sys.executable, "-c", code, self.pidfile],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertEqual(proc.stdout.readline().strip(), bounded_run.STARTED_LINE)
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        # The group may already be gone before the child wrote its pid; if it did, it must end.
        time.sleep(bounded_run.GRACE + 2)
        try:
            child = int(Path(self.pidfile).read_text())
        except (OSError, ValueError):
            return
        self.assertFalse(alive(child))


class WatchdogHandOverTests(unittest.TestCase):
    """The watchdog with a real program, driven step by step (2.6.12)."""

    def setUp(self):
        import socket
        self.info_parent, self.info_child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.alive_r, self.alive_w = os.pipe()
        self.addCleanup(self.close_all)
        self.kids = []
        self.addCleanup(self.kill_kids)

    def close_all(self):
        for sock in (self.info_parent, self.info_child):
            sock.close()
        for fd in (self.alive_r, self.alive_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def kill_kids(self):
        for pid in self.kids:
            if alive(pid):
                os.kill(pid, signal.SIGKILL)

    def start_program(self, ignore_term=True):
        """A program started as the supervisor starts it; it leaves a child in its group and exits."""
        body = "import signal,time\n" + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "") + \
            "time.sleep(120)\n"
        code = ("import subprocess,sys;p=subprocess.Popen([sys.executable,'-c',%r]);print(p.pid,flush=True)" % body)
        leader = subprocess.Popen([sys.executable, "-c", code], start_new_session=True, stdout=subprocess.PIPE,
                                  text=True, preexec_fn=bounded_run._child_setup(os.getpid(), self.info_parent))
        child = int(leader.stdout.readline())
        leader.stdout.close()
        self.kids.append(child)
        return leader, child

    def run_watchdog(self, grace=0.5):
        import threading
        thread = threading.Thread(target=bounded_run.watchdog, args=(self.info_child, self.alive_r, grace))
        thread.start()
        return thread

    def wait_gone(self, pid, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not alive(pid):
                return True
            time.sleep(0.05)
        return False

    def test_the_leader_reaped_before_the_watchdog_reads_its_message(self):
        # 2.6.11 read the group from the pidfd's fdinfo, which says "Pid: -1" once the leader is
        # reaped, and gave up before any group signal: the child outlived the supervisor.
        leader, child = self.start_program()
        leader.wait()                                         # reaped (as init does)
        os.close(self.alive_w)                               # the supervisor died: no "done"
        self.alive_w = -1
        thread = self.run_watchdog()
        thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertTrue(self.wait_gone(child, 3))

    def test_a_done_with_something_left_in_the_group_still_ends_it(self):
        leader, child = self.start_program(ignore_term=False)
        os.write(self.alive_w, b"d")                         # a "done" that is not true
        thread = self.run_watchdog()
        thread.join(15)
        self.assertTrue(self.wait_gone(child, 3))
        leader.wait()

    def test_a_true_done_ends_nothing(self):
        leader, child = self.start_program(ignore_term=False)
        os.kill(child, signal.SIGKILL)
        self.assertTrue(self.wait_gone(child, 3))
        sent = []
        original = bounded_run._group_signal
        bounded_run._group_signal = lambda *a: sent.append(a) or original(*a)
        try:
            os.write(self.alive_w, b"d")
            thread = self.run_watchdog()
            thread.join(15)
        finally:
            bounded_run._group_signal = original
        self.assertEqual(sent, [])
        leader.wait()

    def test_no_message_and_hostile_messages(self):
        self.assertIsNone(bounded_run._parse_info(b""))
        self.assertIsNone(bounded_run._parse_info(b"p"))
        self.assertIsNone(bounded_run._parse_info(b"p1"))            # never init
        self.assertIsNone(bounded_run._parse_info(b"p-5"))
        self.assertIsNone(bounded_run._parse_info(b"p12a"))
        self.assertIsNone(bounded_run._parse_info(b"p" + b"9" * 20))
        self.assertEqual(bounded_run._parse_info(b"p4242"), 4242)
        self.info_parent.close()                             # the program never started
        os.close(self.alive_w)
        self.alive_w = -1
        thread = self.run_watchdog()
        thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_an_old_kernel_reaches_the_group_by_number_only_while_it_has_members(self):
        import errno
        leader, child = self.start_program()
        pidfd = os.pidfd_open(leader.pid)
        self.addCleanup(os.close, pidfd)
        leader.wait()                                         # reaped: the pidfd alone proves nothing
        real = signal.pidfd_send_signal

        def old_kernel(fd, sig, info=None, flags=0):
            if flags:
                raise OSError(errno.EINVAL, "flags")
            return real(fd, sig, info, flags)
        signal.pidfd_send_signal = old_kernel
        try:
            self.assertTrue(bounded_run._group_left(pidfd, leader.pid))
            self.assertTrue(bounded_run._group_signal(pidfd, leader.pid, signal.SIGKILL))
            self.assertTrue(self.wait_gone(child, 3))
            self.assertFalse(bounded_run._group_left(pidfd, leader.pid))
            self.assertFalse(bounded_run._group_signal(pidfd, leader.pid, signal.SIGTERM))   # never a stale number
        finally:
            signal.pidfd_send_signal = real

    def test_the_supervisor_says_done_only_for_an_empty_group(self):
        writes = []
        real_write = os.write

        def record(fd, data):
            writes.append(data)
            return real_write(fd, data)
        with tempfile.TemporaryDirectory() as tmp:
            for empty in (True, False):
                writes.clear()
                original = bounded_run.wait_group_empty
                bounded_run.wait_group_empty = lambda pgid, seconds: empty
                os.write = record
                try:
                    code = bounded_run.run(5, ["/usr/bin/true"], grace=0.2)
                finally:
                    os.write = real_write
                    bounded_run.wait_group_empty = original
                self.assertEqual(code, 0)
                self.assertEqual(b"d" in writes, empty)


class BarWidgetUsesTheSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.qml = (ROOT / "BarWidget.qml").read_text()

    def test_no_helper_runs_under_foreground_timeout_any_more(self):
        self.assertNotIn("--foreground", self.qml)
        self.assertIn('root.boundedPath, String(seconds), "--"', self.qml)

    def test_every_finite_helper_is_bounded(self):
        # Each python helper the widget runs goes through bounded(); none is started bare.
        for line in self.qml.splitlines():
            if '"/usr/bin/python3"' in line and "boundedPath" not in line:
                self.assertIn("root.bounded(", line, line.strip())

    def test_outer_deadlines_leave_room_for_the_supervisor_and_its_grace(self):
        grace = bounded_run.GRACE
        helper_limit = int(re.search(r"helperLimit: (\d+)", self.qml).group(1))
        tiling_limit = int(re.search(r"tilingLimit: (\d+)", self.qml).group(1))
        helper_outer = int(re.search(r"id: helperDeadline\s+interval: (\d+)", self.qml).group(1))
        tiling_outer = int(re.search(r"id: tilingDeadline\s+interval: (\d+)", self.qml).group(1))
        self.assertGreater(helper_outer / 1000, helper_limit + grace)
        self.assertGreater(tiling_outer / 1000, tiling_limit + grace)
        self.assertGreater(helper_limit, 120 + 60)     # theme switch 120 s after a 60 s lock wait

    def test_a_window_counts_as_handed_over_only_once_its_helper_started(self):
        start = self.qml.index("function nextTilingWindow()")
        body = self.qml[start:self.qml.index("property bool tilingBusy", start)]
        self.assertNotIn("markTilingSeen", body)
        self.assertNotIn("onStarted: root.tilingHelperStarted()", self.qml)   # supervisor only
        self.assertIn('"--report-start", String(tilingLimit), "--"', self.qml)
        self.assertIn('data === "%s") root.tilingHelperStarted()' % bounded_run.STARTED_LINE, self.qml)
        started = self.qml[self.qml.index("function tilingHelperStarted()"):]
        self.assertIn("markTilingSeen(tilingCurrent)", started[:300])
        settled = self.qml[self.qml.index("function tilingSettled()"):]
        settled = settled[:settled.index("\n  }\n")]
        self.assertIn("if (!tilingStarted)", settled)
        self.assertIn("tilingRetry.restart()", settled)


if __name__ == "__main__":
    unittest.main()
