"""Shared constants and path helpers for the tools module."""

import fnmatch

# Base directory names to exclude when walking the filesystem
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "build",
        "dist",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".eggs",
    }
)

# For backward compatibility
EXCLUDED_DIRS = EXCLUDED_DIR_NAMES


def is_excluded_dir(dir_name: str) -> bool:
    """Returns True if the directory name matches any excluded patterns."""
    if dir_name in EXCLUDED_DIR_NAMES:
        return True
    if dir_name.endswith(".egg-info") or dir_name.endswith(".dist-info"):
        return True
    if fnmatch.fnmatch(dir_name, "*.egg-info"):
        return True
    return False
