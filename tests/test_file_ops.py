from __future__ import annotations

from pathlib import Path
import unittest

from assistant.tools import file_ops


class FileOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project_root = Path("tests/.tmp/file_ops_workspace").resolve()
        self.project_root.mkdir(parents=True, exist_ok=True)
        (self.project_root / "notes.txt").unlink(missing_ok=True)

    def test_create_and_read_file_within_workspace(self) -> None:
        created = file_ops.create_file("notes.txt", project_root=self.project_root, content="hello")
        self.assertTrue(created.success)
        self.assertIn("path", created.data)

        read = file_ops.read_file("notes.txt", project_root=self.project_root)
        self.assertTrue(read.success)
        self.assertEqual(read.data["content"], "hello")

    def test_rejects_path_outside_safe_roots(self) -> None:
        unsafe_path = Path(self.project_root.anchor) / "Windows" / "System32" / "drivers" / "etc" / "hosts"
        result = file_ops.read_file(str(unsafe_path), project_root=self.project_root)
        self.assertFalse(result.success)
        self.assertEqual(result.error, "unsafe_path")


if __name__ == "__main__":
    unittest.main()
