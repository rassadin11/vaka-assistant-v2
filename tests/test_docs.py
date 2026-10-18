"""Documentation integrity: relative links resolve and live docs are indexed (plan D-38)."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Generated or third-party trees, plus agent worktrees that hold whole repo copies.
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    ".uv-cache",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "node_modules",
    "dist",
    "worktrees",
}

LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)

# Each index must link every document that lives directly in the listed folders.
# Archive folders are deliberately absent: an index points at them as a whole.
INDEXES: dict[Path, tuple[Path, ...]] = {
    ROOT / "plan" / "README.md": (ROOT / "plan", ROOT / "plan" / "specs"),
    ROOT / "docs" / "README.md": (ROOT / "docs",),
}


def _markdown_files() -> list[Path]:
    found: list[Path] = []
    for current, dirs, files in os.walk(ROOT):
        dirs[:] = [name for name in dirs if name not in EXCLUDED_DIRS]
        found.extend(Path(current) / name for name in files if name.endswith(".md"))
    return sorted(found)


def _relative_links(path: Path) -> list[str]:
    """Return local link targets without anchors; code blocks are not documentation links."""

    text = FENCE.sub("", path.read_text(encoding="utf-8"))
    targets: list[str] = []
    for target in LINK.findall(text):
        if SCHEME.match(target) or target.startswith("#"):
            continue
        local = target.split("#", 1)[0]
        if local:
            targets.append(local)
    return targets


def test_relative_markdown_links_resolve() -> None:
    broken = [
        f"{path.relative_to(ROOT).as_posix()} -> {target}"
        for path in _markdown_files()
        for target in _relative_links(path)
        if not (path.parent / target).exists()
    ]

    assert not broken, "broken relative links:\n" + "\n".join(broken)


@pytest.mark.parametrize(
    "index",
    sorted(INDEXES),
    ids=lambda path: path.relative_to(ROOT).as_posix(),
)
def test_every_live_document_is_indexed(index: Path) -> None:
    linked = {(index.parent / target).resolve() for target in _relative_links(index)}
    missing = [
        document.relative_to(ROOT).as_posix()
        for folder in INDEXES[index]
        for document in sorted(folder.glob("*.md"))
        if document != index and document.resolve() not in linked
    ]

    assert not missing, f"not linked from {index.relative_to(ROOT).as_posix()}:\n" + "\n".join(
        missing
    )
