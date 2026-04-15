import unittest
from pathlib import Path

from src.config import RunnerSettings


class RunnerConfigDefaultsTests(unittest.TestCase):
    def test_qemu_ssh_user_default_matches_env_example_and_compose(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        runner_root = repo_root / "runner"

        default_user = RunnerSettings().qemu_ssh_user
        self.assertEqual(default_user, "root")

        env_example = (runner_root / ".env.example").read_text(encoding="utf-8")
        self.assertIn(f"RUNNER_QEMU_SSH_USER={default_user}", env_example)

        compose = (runner_root / "compose.yml").read_text(encoding="utf-8")
        self.assertIn(
            f"RUNNER_QEMU_SSH_USER: ${{RUNNER_QEMU_SSH_USER:-{default_user}}}",
            compose,
        )


if __name__ == "__main__":
    unittest.main()
