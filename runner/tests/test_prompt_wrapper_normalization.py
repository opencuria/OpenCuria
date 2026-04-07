import shlex
import unittest

from src.config import RunnerSettings
from src.service import WorkspaceService


class PromptWrapperNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = WorkspaceService({}, RunnerSettings())

    def _build_wrapped_args(self, command_args, task_id: str = "task-123"):
        pid_file = f"/tmp/opencuria-prompt-{task_id}.pid"
        normalised_args = self.service._normalise_command_args(command_args)
        wrapped_entrypoint = (
            f"printf '%s' \"$$\" > {shlex.quote(pid_file)}; exec \"$@\""
        )
        return [
            "sh",
            "-lc",
            wrapped_entrypoint,
            "opencuria-prompt",
            *normalised_args,
        ]

    def test_wraps_codex_exec_with_attached_redirection(self) -> None:
        wrapped = self._build_wrapped_args(
            [
                "codex",
                "exec",
                "--json",
                "hello",
                "2>/dev/null",
            ]
        )

        self.assertEqual(wrapped[:4], ["sh", "-lc", wrapped[2], "opencuria-prompt"])
        self.assertEqual(wrapped[4:6], ["bash", "-lc"])
        self.assertIn("codex exec --json hello 2>/dev/null", wrapped[6])
        self.assertNotIn("'2>/dev/null'", wrapped[6])

    def test_wraps_plain_argv_without_unnecessary_shell_layer(self) -> None:
        wrapped = self._build_wrapped_args(["python", "-V"])

        self.assertEqual(wrapped[4:], ["python", "-V"])


if __name__ == "__main__":
    unittest.main()
