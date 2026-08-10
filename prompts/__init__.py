"""Loader for system prompts stored as plain-text files in this directory."""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    """Read prompts/<name>.md and return its contents, trailing newline stripped."""
    return (PROMPTS_DIR / f"{name}.md").read_text().rstrip("\n")
