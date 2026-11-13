import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class BatchDocs(unittest.TestCase):
    def test_readme_documents_batching(self):
        t = (ROOT / "README.md").read_text()
        self.assertIn("Batches and parallel work", t)
        self.assertIn("--fresh", t)

    def test_design_note_kept(self):
        self.assertIn("One live session per professional name", (ROOT / "docs" / "design.md").read_text())


if __name__ == "__main__":
    unittest.main()
