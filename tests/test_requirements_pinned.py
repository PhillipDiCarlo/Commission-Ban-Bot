"""
Sanity check for bug #5 in NOTES.md: requirements.txt previously had no
version pins at all, so a plain `pip install` (including inside `docker
build`) could silently pull a breaking major version on a future rebuild.
This just guards against that regressing.

Run via (from repo root):
    .venv\\Scripts\\python.exe -m unittest tests.test_requirements_pinned -v
"""
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RequirementsPinnedTests(unittest.TestCase):
    def test_every_requirement_line_is_pinned(self):
        path = os.path.join(REPO_ROOT, "requirements.txt")
        with open(path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]

        self.assertTrue(lines, "requirements.txt should not be empty")
        for line in lines:
            self.assertIn("==", line, f"{line!r} is not pinned to an exact version")


if __name__ == "__main__":
    unittest.main()
