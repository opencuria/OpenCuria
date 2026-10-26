import unittest

from src.services.workspace_helpers import (
    sanitize_exec_workdir,
    sanitize_path,
)


class PathValidationTests(unittest.TestCase):
    def test_sanitize_path_allows_workspace_root_and_children(self) -> None:
        self.assertEqual(sanitize_path("/workspace"), "/workspace")
        self.assertEqual(
            sanitize_path("/workspace/project/file.txt"),
            "/workspace/project/file.txt",
        )

    def test_sanitize_path_rejects_prefix_collisions_outside_workspace(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_path("/workspace-entrypoint.sh")

        with self.assertRaises(ValueError):
            sanitize_path("/workspace_backup/secrets.txt")

    def test_sanitize_exec_workdir_allows_tmp(self) -> None:
        self.assertEqual(sanitize_exec_workdir("/tmp"), "/tmp")
        self.assertEqual(sanitize_exec_workdir(""), "/workspace")
        self.assertEqual(sanitize_exec_workdir("src"), "/workspace/src")

    def test_sanitize_exec_workdir_rejects_newlines(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_exec_workdir("/tmp\n")

if __name__ == "__main__":
    unittest.main()
