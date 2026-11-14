#!/usr/bin/env python3
"""
Obsidian Vault Orphaned Image Analyzer & Cleaner
Supports Interactive CLI Mode and Native Tkinter GUI Mode.
"""

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple
from urllib.parse import unquote

# Optional dependencies for enhanced UX and OS Trash support
try:
    from send2trash import send2trash
    HAS_SEND2TRASH = True
except ImportError:
    HAS_SEND2TRASH = False

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    HAS_RICH = True
    console = Console()
except ImportError:
    HAS_RICH = False


# =====================================================================
# 1. CORE CONFIGURATION & DATA STRUCTURES
# =====================================================================

DEFAULT_IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp", ".avif", ".tif", ".tiff", ".ico"
}

DEFAULT_IGNORED_DIRS = {
    ".obsidian", ".trash", ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv"
}

TEXT_SCAN_EXTENSIONS = {".md", ".canvas"}


@dataclass
class ImageFile:
    abs_path: Path
    rel_path: str          # POSIX-normalized relative path from vault root (e.g., "assets/img.png")
    name: str              # Basename (e.g., "img.png")
    size_bytes: int

    @property
    def rel_lower(self) -> str:
        return self.rel_path.lower()

    @property
    def name_lower(self) -> str:
        return self.name.lower()


@dataclass
class AuditReport:
    vault_path: Path
    total_notes_scanned: int = 0
    total_images: List[ImageFile] = field(default_factory=list)
    used_images: List[ImageFile] = field(default_factory=list)
    orphaned_images: List[ImageFile] = field(default_factory=list)

    @property
    def total_orphan_size_bytes(self) -> int:
        return sum(img.size_bytes for img in self.orphaned_images)


def format_bytes(size: int) -> str:
    """Convert bytes to human-readable string."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"


# =====================================================================
# 2. CORE SCANNER & PARSER ENGINE
# =====================================================================

class VaultAuditor:
    """Scans an Obsidian Vault to identify used and orphaned image files."""

    # 1. WikiLinks: ![[path/to/image.png|alt|300]] or [[path/to/image.png]]
    RE_WIKILINK = re.compile(r"!?\[\[([^\]|#\n]+)(?:#[^\]|\n]*)?(?:\|[^\]\n]*)?\]\]")

    # 2. Standard Markdown Links: ![alt](<path with spaces.png> "title") or [alt](path%20encoded.png)
    RE_MDLINK = re.compile(r"!?\[[^\]]*\]\(\s*(?:<([^>\n]+)>|([^\s)\n]+))(?:\s+[\"\'][^\"\']*[\"\'])?\s*\)")

    # 3. HTML <img src="..."> tags
    RE_HTML_IMG = re.compile(r"<img\s+[^>]*src=[\"\']([^\"\']+)[\"\'][^>]*>", re.IGNORECASE)

    # 4. Raw Frontmatter / Inline strings ending in image extensions (e.g., banner: "assets/bg.png")
    RE_RAW_PATH = re.compile(
        r"[\"'`]?([a-zA-Z0-9_\-\./\\ ]+\.(?:png|jpe?g|gif|bmp|svg|webp|avif|tiff?|ico))[\"'`]?",
        re.IGNORECASE
    )

    def __init__(
        self,
        vault_path: Path,
        image_extensions: Set[str] = DEFAULT_IMAGE_EXTENSIONS,
        ignored_dirs: Set[str] = DEFAULT_IGNORED_DIRS,
    ):
        self.vault_path = vault_path.resolve()
        self.image_extensions = {ext.lower() for ext in image_extensions}
        self.ignored_dirs = ignored_dirs

    def _is_external_link(self, link: str) -> bool:
        link_lower = link.lower()
        return link_lower.startswith(("http://", "https://", "data:", "ftp://", "mailto:", "obsidian://"))

    def _clean_extracted_target(self, raw_target: str) -> str:
        """Normalizes a raw link target extracted from Markdown/Canvas."""
        target = raw_target.strip()
        target = unquote(target)
        target = target.split("#")[0].split("?")[0].strip()
        target = target.replace("\\", "/")
        while target.startswith("./") or target.startswith("/"):
            target = target.lstrip("./")
        return target

    def extract_references_from_text(self, content: str, is_canvas: bool = False) -> Set[str]:
        """Extracts all normalized candidate file references from a note or canvas file."""
        refs: Set[str] = set()

        if is_canvas:
            try:
                data = json.loads(content)
                for node in data.get("nodes", []):
                    if node.get("type") == "file" and "file" in node:
                        refs.add(self._clean_extracted_target(node["file"]))
            except json.JSONDecodeError:
                pass
            return refs

        # 1. WikiLinks
        for match in self.RE_WIKILINK.finditer(content):
            raw = match.group(1)
            if raw and not self._is_external_link(raw):
                refs.add(self._clean_extracted_target(raw))

        # 2. Markdown Links
        for match in self.RE_MDLINK.finditer(content):
            raw = match.group(1) or match.group(2)
            if raw and not self._is_external_link(raw):
                refs.add(self._clean_extracted_target(raw))

        # 3. HTML <img> tags
        for match in self.RE_HTML_IMG.finditer(content):
            raw = match.group(1)
            if raw and not self._is_external_link(raw):
                refs.add(self._clean_extracted_target(raw))

        # 4. Raw Frontmatter / Dataview paths
        for match in self.RE_RAW_PATH.finditer(content):
            raw = match.group(1)
            if raw and not self._is_external_link(raw):
                refs.add(self._clean_extracted_target(raw))

        return refs

    def scan(self) -> AuditReport:
        """Walks the vault, indexes all images and text files, and computes orphans."""
        if not self.vault_path.exists() or not self.vault_path.is_dir():
            raise FileNotFoundError(f"Vault directory not found: {self.vault_path}")

        all_images: List[ImageFile] = []
        note_files: List[Path] = []

        # Step 1: Walk directory tree and classify files
        for root, dirs, files in os.walk(self.vault_path):
            dirs[:] = [
                d for d in dirs
                if d not in self.ignored_dirs and not d.startswith(".")
            ]

            root_path = Path(root)
            for fname in files:
                fpath = root_path / fname
                ext = fpath.suffix.lower()

                if ext in self.image_extensions:
                    rel_posix = fpath.relative_to(self.vault_path).as_posix()
                    try:
                        fsize = fpath.stat().st_size
                    except OSError:
                        fsize = 0
                    all_images.append(
                        ImageFile(abs_path=fpath, rel_path=rel_posix, name=fname, size_bytes=fsize)
                    )
                elif ext in TEXT_SCAN_EXTENSIONS:
                    note_files.append(fpath)

        # Step 2: Parse all notes and resolve links
        global_path_refs: Set[str] = set()
        global_basename_refs: Set[str] = set()

        for note_path in note_files:
            try:
                content = note_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            is_canvas = note_path.suffix.lower() == ".canvas"
            extracted = self.extract_references_from_text(content, is_canvas=is_canvas)
            note_dir = note_path.parent

            for ref in extracted:
                ref_lower = ref.lower()
                if "/" in ref_lower:
                    try:
                        resolved = (note_dir / ref).resolve()
                        if resolved.is_relative_to(self.vault_path):
                            rel_from_vault = resolved.relative_to(self.vault_path).as_posix().lower()
                            global_path_refs.add(rel_from_vault)
                    except (ValueError, OSError):
                        pass
                    global_path_refs.add(ref_lower)
                else:
                    global_basename_refs.add(ref_lower)

        # Step 3: Match images against references
        used_images: List[ImageFile] = []
        orphaned_images: List[ImageFile] = []

        for img in sorted(all_images, key=lambda x: x.rel_lower):
            is_used = False

            if img.rel_lower in global_path_refs:
                is_used = True
            else:
                for pref in global_path_refs:
                    if img.rel_lower.endswith("/" + pref):
                        is_used = True
                        break

            if not is_used and img.name_lower in global_basename_refs:
                is_used = True

            if is_used:
                used_images.append(img)
            else:
                orphaned_images.append(img)

        return AuditReport(
            vault_path=self.vault_path,
            total_notes_scanned=len(note_files),
            total_images=all_images,
            used_images=used_images,
            orphaned_images=orphaned_images,
        )

    def delete_images(
        self,
        targets: List[ImageFile],
        mode: str = "obsidian_trash",
        dry_run: bool = False
    ) -> Tuple[int, int, List[str]]:
        """
        Deletes or trashes the specified images.
        Modes:
          - 'obsidian_trash': Moves files to <vault>/.trash/ (preserves folder structure)
          - 'os_trash': Sends files to OS Recycle Bin via send2trash (falls back to obsidian_trash)
          - 'permanent': Permanently unlinks files from disk
        Returns: (deleted_count, bytes_freed, errors)
        """
        deleted_count = 0
        bytes_freed = 0
        errors: List[str] = []

        obsidian_trash_dir = self.vault_path / ".trash"

        for img in targets:
            if not img.abs_path.exists():
                continue

            if dry_run:
                deleted_count += 1
                bytes_freed += img.size_bytes
                continue

            try:
                if mode == "os_trash" and HAS_SEND2TRASH:
                    send2trash(str(img.abs_path))
                elif mode in ("obsidian_trash", "os_trash"):
                    dest = obsidian_trash_dir / img.rel_path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        stem, suffix = dest.stem, dest.suffix
                        counter = 1
                        while dest.exists():
                            dest = dest.with_name(f"{stem}_{counter}{suffix}")
                            counter += 1
                    shutil.move(str(img.abs_path), str(dest))
                elif mode == "permanent":
                    img.abs_path.unlink()
                else:
                    raise ValueError(f"Unknown deletion mode: {mode}")

                deleted_count += 1
                bytes_freed += img.size_bytes
            except Exception as e:
                errors.append(f"{img.rel_path}: {str(e)}")

        return deleted_count, bytes_freed, errors


# =====================================================================
# 3. INTERACTIVE CLI INTERFACE
# =====================================================================

def run_interactive_cli(auditor: VaultAuditor, delete_mode: str, dry_run: bool):
    """Runs the interactive CLI workflow."""
    print(f"\nScanning Obsidian Vault: {auditor.vault_path} ...")
    report = auditor.scan()

    if HAS_RICH:
        summary_text = (
            f"[bold cyan]Vault Path:[/bold cyan] {report.vault_path}\n"
            f"[bold green]Notes / Canvases Scanned:[/bold green] {report.total_notes_scanned}\n"
            f"[bold blue]Total Images Found:[/bold blue] {len(report.total_images)}\n"
            f"[bold emerald]Referenced (Used) Images:[/bold emerald] {len(report.used_images)}\n"
            f"[bold red]Orphaned Images:[/bold red] {len(report.orphaned_images)} "
            f"({format_bytes(report.total_orphan_size_bytes)})"
        )
        console.print(Panel(summary_text, title="Obsidian Vault Image Audit", expand=False))
    else:
        print("=" * 60)
        print(f"Vault Path               : {report.vault_path}")
        print(f"Notes / Canvases Scanned : {report.total_notes_scanned}")
        print(f"Total Images Found       : {len(report.total_images)}")
        print(f"Referenced Images        : {len(report.used_images)}")
        print(f"Orphaned Images          : {len(report.orphaned_images)} ({format_bytes(report.total_orphan_size_bytes)})")
        print("=" * 60)

    if not report.orphaned_images:
        print("\nNo orphaned images found! Your vault is clean.")
        return

    if HAS_RICH:
        table = Table(title="Orphaned Images Detected")
        table.add_column("#", style="dim", width=5, justify="right")
        table.add_column("Relative Path in Vault", style="yellow")
        table.add_column("Size", justify="right", style="cyan")
        for idx, img in enumerate(report.orphaned_images, start=1):
            table.add_row(str(idx), img.rel_path, format_bytes(img.size_bytes))
        console.print(table)
    else:
        print("\nOrphaned Images:")
        for idx, img in enumerate(report.orphaned_images, start=1):
            print(f"  [{idx:3d}] {img.rel_path} ({format_bytes(img.size_bytes)})")

    while True:
        print("\nOptions:")
        print("  [A] Delete ALL orphaned images")
        print("  [S] Select specific orphaned images by number (e.g., 1,3,5-8)")
        print("  [E] Export orphan report to JSON")
        print("  [M] Change deletion mode (Current: " + delete_mode + ")")
        print("  [Q] Quit without deleting")

        choice = input("\nSelect an action [A/S/E/M/Q]: ").strip().upper()

        if choice == "Q":
            print("Exiting. No files were modified.")
            break

        elif choice == "M":
            print("\nAvailable Deletion Modes:")
            print("  1. obsidian_trash (Moves to <vault>/.trash/ - Recommended)")
            print("  2. os_trash       (Moves to OS Recycle Bin; requires send2trash)")
            print("  3. permanent      (Permanently deletes from disk - Irrecoverable)")
            m_choice = input("Choose mode [1/2/3]: ").strip()
            if m_choice == "1":
                delete_mode = "obsidian_trash"
            elif m_choice == "2":
                delete_mode = "os_trash"
            elif m_choice == "3":
                delete_mode = "permanent"
            print(f"Deletion mode set to: {delete_mode}")

        elif choice == "E":
            out_file = report.vault_path / "orphan_images_report.json"
            payload = {
                "vault": str(report.vault_path),
                "orphaned_count": len(report.orphaned_images),
                "total_orphan_bytes": report.total_orphan_size_bytes,
                "orphans": [
                    {"path": img.rel_path, "size_bytes": img.size_bytes}
                    for img in report.orphaned_images
                ],
            }
            out_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"Report exported to: {out_file}")

        elif choice in ("A", "S"):
            targets: List[ImageFile] = []
            if choice == "A":
                targets = report.orphaned_images
            else:
                raw_sel = input("Enter indices to delete (e.g., 1,2,5-10): ").strip()
                selected_indices = parse_index_selection(raw_sel, len(report.orphaned_images))
                targets = [report.orphaned_images[i - 1] for i in sorted(selected_indices)]

            if not targets:
                print("No valid images selected.")
                continue

            total_sz = format_bytes(sum(t.size_bytes for t in targets))
            prefix = "[DRY-RUN] " if dry_run else ""
            confirm = input(
                f"{prefix}Confirm deleting {len(targets)} file(s) ({total_sz}) via '{delete_mode}'? [y/N]: "
            ).strip().lower()

            if confirm == "y":
                count, freed, errs = auditor.delete_images(targets, mode=delete_mode, dry_run=dry_run)
                print(f"\n{prefix}Processed {count} file(s), reclaimed {format_bytes(freed)}.")
                if errs:
                    print("Errors encountered:")
                    for e in errs:
                        print(f"  - {e}")
                break


def parse_index_selection(expr: str, max_idx: int) -> Set[int]:
    """Parses strings like '1, 3, 5-8' into a validated set of 1-based indices."""
    result: Set[int] = set()
    for part in expr.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-", 1)
            if bounds[0].isdigit() and bounds[1].isdigit():
                start, end = int(bounds[0]), int(bounds[1])
                for idx in range(min(start, end), max(start, end) + 1):
                    if 1 <= idx <= max_idx:
                        result.add(idx)
        elif part.isdigit():
            idx = int(part)
            if 1 <= idx <= max_idx:
                result.add(idx)
    return result


# =====================================================================
# 4. GRAPHICAL USER INTERFACE (TKINTER)
# =====================================================================

def run_gui(initial_vault_path: Path = None):
    """Launches the native Tkinter GUI for auditing and deleting orphaned images."""
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    class ObsidianAuditorGUI(tk.Tk):
        def __init__(self, start_path: Path = None):
            super().__init__()
            self.title("Obsidian Vault Orphaned Image Auditor")
            self.geometry("950x620")
            self.minsize(750, 480)

            self.auditor: VaultAuditor = None
            self.report: AuditReport = None
            self.item_map: Dict[str, ImageFile] = {}

            self._build_ui(start_path)

        def _build_ui(self, start_path: Path):
            top_frame = ttk.Frame(self, padding=10)
            top_frame.pack(fill=tk.X)

            ttk.Label(top_frame, text="Vault Folder:").pack(side=tk.LEFT)
            self.path_var = tk.StringVar(value=str(start_path) if start_path else "")
            path_entry = ttk.Entry(top_frame, textvariable=self.path_var)
            path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)

            ttk.Button(top_frame, text="Browse...", command=self._browse_vault).pack(side=tk.LEFT, padx=3)
            ttk.Button(top_frame, text="Scan Vault", command=self._scan_vault).pack(side=tk.LEFT, padx=3)

            self.stats_var = tk.StringVar(value="Select an Obsidian vault folder and click 'Scan Vault'.")
            stats_label = ttk.Label(self, textvariable=self.stats_var, padding=(10, 4), font=("Segoe UI", 10, "bold"))
            stats_label.pack(fill=tk.X)

            table_frame = ttk.Frame(self, padding=(10, 5))
            table_frame.pack(fill=tk.BOTH, expand=True)

            columns = ("rel_path", "filename", "size")
            self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
            self.tree.heading("rel_path", text="Relative Path in Vault")
            self.tree.heading("filename", text="File Name")
            self.tree.heading("size", text="Size")

            self.tree.column("rel_path", width=540, anchor=tk.W)
            self.tree.column("filename", width=220, anchor=tk.W)
            self.tree.column("size", width=110, anchor=tk.E)

            vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=vsb.set)

            self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            vsb.pack(side=tk.RIGHT, fill=tk.Y)

            bottom_frame = ttk.Frame(self, padding=10)
            bottom_frame.pack(fill=tk.X)

            ttk.Label(bottom_frame, text="Deletion Mode:").pack(side=tk.LEFT)
            self.mode_var = tk.StringVar(value="obsidian_trash")
            mode_combo = ttk.Combobox(
                bottom_frame,
                textvariable=self.mode_var,
                values=["obsidian_trash", "os_trash", "permanent"],
                state="readonly",
                width=16,
            )
            mode_combo.pack(side=tk.LEFT, padx=6)

            self.dry_run_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(bottom_frame, text="Dry Run (Simulate)", variable=self.dry_run_var).pack(side=tk.LEFT, padx=8)

            ttk.Button(bottom_frame, text="Delete All Orphans", command=self._delete_all).pack(side=tk.RIGHT, padx=4)
            ttk.Button(bottom_frame, text="Delete Selected", command=self._delete_selected).pack(side=tk.RIGHT, padx=4)
            ttk.Button(bottom_frame, text="Select All", command=self._select_all).pack(side=tk.RIGHT, padx=4)

        def _browse_vault(self):
            folder = filedialog.askdirectory(title="Select Obsidian Vault Root Directory")
            if folder:
                self.path_var.set(folder)
                self._scan_vault()

        def _scan_vault(self):
            raw_path = self.path_var.get().strip()
            if not raw_path:
                messagebox.showwarning("Missing Path", "Please select a valid Obsidian vault directory first.")
                return

            vpath = Path(raw_path)
            if not vpath.exists() or not vpath.is_dir():
                messagebox.showerror("Invalid Directory", f"Directory does not exist:\n{vpath}")
                return

            self.auditor = VaultAuditor(vpath)
            self.report = self.auditor.scan()

            for item in self.tree.get_children():
                self.tree.delete(item)
            self.item_map.clear()

            for img in self.report.orphaned_images:
                iid = self.tree.insert("", tk.END, values=(img.rel_path, img.name, format_bytes(img.size_bytes)))
                self.item_map[iid] = img

            self.stats_var.set(
                f"Notes Scanned: {self.report.total_notes_scanned}   |   "
                f"Total Images: {len(self.report.total_images)}   |   "
                f"Used: {len(self.report.used_images)}   |   "
                f"Orphaned: {len(self.report.orphaned_images)} ({format_bytes(self.report.total_orphan_size_bytes)})"
            )

        def _select_all(self):
            self.tree.selection_set(self.tree.get_children())

        def _delete_selected(self):
            selected_iids = self.tree.selection()
            if not selected_iids:
                messagebox.showinfo("No Selection", "Please select one or more orphaned images from the list.")
                return
            targets = [self.item_map[iid] for iid in selected_iids if iid in self.item_map]
            self._execute_deletion(targets)

        def _delete_all(self):
            if not self.report or not self.report.orphaned_images:
                messagebox.showinfo("Clean Vault", "There are no orphaned images to delete.")
                return
            self._execute_deletion(list(self.report.orphaned_images))

        def _execute_deletion(self, targets: List[ImageFile]):
            mode = self.mode_var.get()
            dry_run = self.dry_run_var.get()
            total_sz = format_bytes(sum(t.size_bytes for t in targets))

            prefix = "[DRY RUN] " if dry_run else ""
            msg = (
                f"{prefix}Are you sure you want to delete {len(targets)} orphaned image(s) ({total_sz})?\n\n"
                f"Mode: {mode}"
            )
            if not messagebox.askyesno("Confirm Action", msg):
                return

            count, freed, errs = self.auditor.delete_images(targets, mode=mode, dry_run=dry_run)
            result_msg = f"{prefix}Processed {count} image(s).\nSpace Reclaimed: {format_bytes(freed)}"
            if errs:
                result_msg += "\n\nErrors:\n" + "\n".join(errs[:10])
            messagebox.showinfo("Operation Complete", result_msg)

            if not dry_run:
                self._scan_vault()

    app = ObsidianAuditorGUI(initial_vault_path)
    app.mainloop()


# =====================================================================
# 5. ENTRY POINT & CLI ARGUMENT PARSER
# =====================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Obsidian Vault Orphaned Image Analyzer & Cleaner (Interactive CLI & GUI)"
    )
    parser.add_argument(
        "vault",
        nargs="?",
        default=None,
        help="Path to the Obsidian Vault root directory.",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Launch the graphical user interface (Tkinter).",
    )
    parser.add_argument(
        "--mode",
        choices=["obsidian_trash", "os_trash", "permanent"],
        default="obsidian_trash",
        help="Deletion mode: moves to <vault>/.trash/ (default), OS Recycle Bin, or permanent deletion.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate deletion without modifying any files on disk.",
    )

    args = parser.parse_args()

    if args.gui:
        start_dir = Path(args.vault).resolve() if args.vault else None
        run_gui(start_dir)
        return

    vault_input = args.vault
    if not vault_input:
        print("=== Obsidian Vault Orphaned Image Auditor ===")
        mode_prompt = input("Choose interface: [1] Interactive CLI (default)  [2] Launch GUI: ").strip()
        if mode_prompt == "2":
            run_gui(None)
            return
        vault_input = input("Enter path to your Obsidian Vault root folder: ").strip().strip("\"'")

    vault_path = Path(vault_input).expanduser().resolve()
    if not vault_path.exists() or not vault_path.is_dir():
        print(f"Error: Directory not found -> {vault_path}", file=sys.stderr)
        sys.exit(1)

    auditor = VaultAuditor(vault_path)
    run_interactive_cli(auditor, delete_mode=args.mode, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
