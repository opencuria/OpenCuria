import unittest

from src.config import RunnerSettings
from src.service import WorkspaceService


class CommandNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = WorkspaceService({}, RunnerSettings())

    def test_detects_attached_stderr_redirection_target(self) -> None:
        args = [
            "ssh-keyscan",
            "-t",
            "ed25519,rsa",
            "github.com",
            ">>",
            "/root/.ssh/known_hosts",
            "2>/dev/null",
        ]

        normalised = self.service._normalise_command_args(args)

        self.assertEqual(normalised[0:2], ["bash", "-lc"])
        self.assertEqual(
            normalised[2],
            "ssh-keyscan -t ed25519,rsa github.com >> /root/.ssh/known_hosts 2>/dev/null",
        )

    def test_detects_embedded_stdout_redirection_target(self) -> None:
        args = ["echo", "hello", ">/tmp/out.log"]

        normalised = self.service._normalise_command_args(args)

        self.assertEqual(normalised[0:2], ["bash", "-lc"])
        self.assertEqual(normalised[2], "echo hello >/tmp/out.log")

    def test_regular_argv_without_shell_tokens_stays_unchanged(self) -> None:
        args = ["python", "-m", "src", "serve"]

        normalised = self.service._normalise_command_args(args)

        self.assertEqual(normalised, args)

    def test_sanitize_path_allows_workspace_root_and_children(self) -> None:
        self.assertEqual(self.service._sanitize_path("/workspace"), "/workspace")
        self.assertEqual(
            self.service._sanitize_path("/workspace/project/file.txt"),
            "/workspace/project/file.txt",
        )

    def test_sanitize_path_rejects_prefix_collisions_outside_workspace(self) -> None:
        with self.assertRaises(ValueError):
            self.service._sanitize_path("/workspace-entrypoint.sh")

        with self.assertRaises(ValueError):
            self.service._sanitize_path("/workspace_backup/secrets.txt")

if __name__ == "__main__":
    unittest.main()
