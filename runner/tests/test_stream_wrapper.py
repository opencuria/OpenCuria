"""Tests for the secure stream wrapper argv builder (no shell interpolation)."""

from __future__ import annotations

import contextlib
import unittest

from src.runtime.stream_wrapper import (
    shell_quote_argv,
    stream_wrapper_argv,
    stream_wrapper_prefix,
)


class StreamWrapperTests(unittest.TestCase):
    def test_prefix_is_static(self) -> None:
        prefix = stream_wrapper_prefix()
        self.assertEqual(prefix[0], "sh")
        self.assertEqual(prefix[1], "-c")
        self.assertEqual(prefix[3], "opencuria-stream")
        self.assertIn("exec setsid --wait", prefix[2])

    def test_wrapper_writes_pidfile_from_inner_setsid_process(self) -> None:
        """pidfile must reference the setsid session leader (GNU setsid
        may fork): ``echo $$`` runs in the inner shell *after* setsid."""
        body = stream_wrapper_prefix()[2]
        self.assertIn("exec setsid --wait sh -c", body)
        self.assertIn('echo $$ > "$1"', body)
        # No user interpolation: the body is fully static.
        self.assertNotIn("--stdio", body)

    def test_setsid_wait_keeps_stdio_attached(self) -> None:
        """``setsid --wait`` (not bare ``setsid``) keeps the direct child
        alive until the server exits: bare ``setsid`` forks on
        util-linux >= 2.35, the parent exits 0 immediately, and the
        transport sees EOF before the server ever speaks (MCP
        ``Connection closed``)."""
        body = stream_wrapper_prefix()[2]
        self.assertIn("setsid --wait", body)
        self.assertNotIn("exec setsid sh -c", body)

    def test_command_appended_verbatim_as_positional_argv(self) -> None:
        evil = "x'; touch /tmp/pwned; echo '"
        argv = stream_wrapper_argv(
            "/tmp/pid", "/workspace", {"A": "b"}, ["echo", evil, "a|b && c"]
        )
        # User values are never joined: they stay separate argv entries.
        self.assertIn("echo", argv)
        self.assertIn(evil, argv)
        self.assertIn("a|b && c", argv)
        # The wrapper body itself contains no user data.
        body = stream_wrapper_prefix()[2]
        self.assertNotIn(evil, body)

    def test_empty_command_rejected(self) -> None:
        with self.assertRaises(ValueError):
            stream_wrapper_argv("/tmp/pid", None, None, [])

    def test_shell_quote_argv_keeps_values_opaque(self) -> None:
        evil = "a'b\"c; rm -rf /"
        quoted = shell_quote_argv(["echo", evil])
        self.assertIn("'a'\"'\"'b\"c; rm -rf /'", quoted)


if __name__ == "__main__":
    unittest.main()


class StreamWrapperLocalProcessTests(unittest.TestCase):
    """Real local subprocess: pidfile = session-leader PID/PGID, killable."""

    def test_wrapper_pidfile_matches_session_leader_and_killable(self) -> None:
        import os
        import signal
        import subprocess
        import tempfile
        import time

        from src.runtime.stream_wrapper import stream_wrapper_argv

        with tempfile.TemporaryDirectory() as tmp:
            pidfile = os.path.join(tmp, "stream-test.pid")
            argv = stream_wrapper_argv(
                pidfile, tmp, {"STREAM_TEST": "ok"},
                ["sh", "-c", "echo started; exec sleep 60"],
            )
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            try:
                deadline = time.time() + 10
                pid = None
                while time.time() < deadline:
                    try:
                        with open(pidfile) as fh:
                            text = fh.read().strip()
                        if text.isdigit():
                            pid = int(text)
                            break
                    except FileNotFoundError:
                        pass
                    time.sleep(0.05)
                self.assertIsNotNone(pid, "pidfile was never written")
                assert pid is not None
                # PID must be alive and lead its own session/process group
                # (setsid session leader), even though GNU setsid forked.
                self.assertEqual(os.getpgid(pid), pid)
                self.assertEqual(os.getsid(pid), pid)

                def _dead(target: int) -> bool:
                    # A reaped-but-unwaited child is a zombie: kill(pid,
                    # 0) still succeeds, so inspect /proc state too.
                    try:
                        with open(f"/proc/{target}/stat") as fh:
                            state = fh.read().rsplit(")", 1)[1].split()[0]
                        if state == "Z":
                            return True
                    except FileNotFoundError:
                        return True
                    try:
                        os.kill(target, 0)
                    except ProcessLookupError:
                        return True
                    except PermissionError:
                        return False
                    # GNU setsid may fork: then our direct child already
                    # exited and the leader is a grandchild — reap it.
                    if proc.poll() is not None:
                        return _dead(target) or True
                    return False

                # The whole tree dies via process-group TERM.
                os.killpg(pid, signal.SIGTERM)
                gone_by = time.time() + 10
                while time.time() < gone_by:
                    if _dead(pid):
                        break
                    # Reap the direct child if it already exited.
                    proc.poll()
                    time.sleep(0.05)
                else:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(pid, signal.SIGKILL)
                    self.fail("stream process group did not die on TERM")
            finally:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
                # Reap pipes to avoid ResourceWarnings.
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()
