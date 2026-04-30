"""Human-editable prompt and instruction loading for Project Sovereign."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


class PromptLibrary:
    """Loads reusable instruction files from disk."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        root = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent / "prompts"
        self.base_dir = root

    def read(self, relative_path: str) -> str:
        path = self.base_dir / relative_path
        return path.read_text(encoding="utf-8").strip()

    def read_many(self, relative_paths: list[str]) -> str:
        blocks = [self.read(path) for path in relative_paths]
        return "\n\n".join(block for block in blocks if block)


@lru_cache(maxsize=1)
def get_prompt_library() -> PromptLibrary:
    """Return a cached prompt library rooted at the repository prompts directory."""

    return PromptLibrary()
