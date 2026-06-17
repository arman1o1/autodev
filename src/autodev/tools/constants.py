"""Shared constants for the tools module."""

# Directories to exclude when walking the filesystem
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "build",
        "dist",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".eggs",
        "*.egg-info",
    }
)
