import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from obsidian_image_auditor import VaultAuditor, parse_index_selection


class TestObsidianVaultAuditor(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp_dir.name)

        # Create standard directories
        (self.vault / "assets").mkdir()
        (self.vault / "subfolder").mkdir()
        (self.vault / "dir1").mkdir()
        (self.vault / "dir2").mkdir()
        (self.vault / ".obsidian" / "plugins").mkdir(parents=True)

        # Create dummy image files
        self.img_wiki = self._create_file("assets/wiki_img.png", b"12345")
        self.img_resized = self._create_file("assets/resized.jpg", b"123456")
        self.img_spaces = self._create_file("assets/Pasted image 2026.png", b"1234567")
        self.img_angle = self._create_file("assets/arch diagram.svg", b"12345678")
        self.img_canvas = self._create_file("assets/canvas_node.webp", b"123456789")
        self.img_dup1 = self._create_file("dir1/dup.png", b"1111")
        self.img_dup2 = self._create_file("dir2/dup.png", b"2222")
        self.img_orphan = self._create_file("assets/unused_orphan.webp", b"999999999")
        self.img_plugin = self._create_file(".obsidian/plugins/icon.png", b"111")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_file(self, rel_path: str, content: bytes) -> Path:
        p = self.vault / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def test_audit_detects_exact_orphans_and_handles_edge_cases(self):
        note_content = """---
banner: "assets/wiki_img.png"
---
# Project Notes
Here is a resized wikilink: ![[resized.jpg|Alt Text|400x200]]
Here is URL-encoded standard link: ![pic](assets/Pasted%20image%202026.png)
Here is angle-bracket link: ![diag](<assets/arch diagram.svg>)
Disambiguated duplicate path: ![[dir1/dup.png]]
External image should be ignored: ![ext](https://example.com/test.png)
"""
        (self.vault / "subfolder" / "note.md").write_text(note_content, encoding="utf-8")

        canvas_content = '{"nodes":[{"id":"1","type":"file","file":"assets/canvas_node.webp"}]}'
        (self.vault / "board.canvas").write_text(canvas_content, encoding="utf-8")

        auditor = VaultAuditor(self.vault)
        report = auditor.scan()

        orphan_rel_paths = [img.rel_path for img in report.orphaned_images]
        used_rel_paths = [img.rel_path for img in report.used_images]

        # Verify only unused_orphan.webp and dir2/dup.png are flagged as orphans
        self.assertEqual(sorted(orphan_rel_paths), ["assets/unused_orphan.webp", "dir2/dup.png"])
        # Verify all 6 linked images are marked used
        self.assertEqual(len(used_rel_paths), 6)
        # Verify .obsidian plugin image was completely ignored
        all_rel_paths = [img.rel_path for img in report.total_images]
        self.assertNotIn(".obsidian/plugins/icon.png", all_rel_paths)

    def test_safe_obsidian_trash_deletion(self):
        auditor = VaultAuditor(self.vault)
        report = auditor.scan()

        count, freed, errors = auditor.delete_images(
            report.orphaned_images, mode="obsidian_trash", dry_run=False
        )
        self.assertEqual(len(errors), 0)
        self.assertTrue((self.vault / ".trash" / "assets" / "unused_orphan.webp").exists())
        self.assertFalse(self.img_orphan.exists())

    def test_index_selection_parser(self):
        indices = parse_index_selection("1, 3, 5-7, 99", max_idx=10)
        self.assertEqual(indices, {1, 3, 5, 6, 7})


if __name__ == "__main__":
    unittest.main()
