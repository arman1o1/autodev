import base64
import logging
import os
import fnmatch
from autodev.tools.constants import EXCLUDED_DIRS

logger = logging.getLogger("autodev.tools.file_ops")


def safe_resolve_path(workspace_root: str, relative_path: str) -> str:
    """Safely resolves target path within workspace root, preventing path traversal."""
    resolved_root = os.path.abspath(workspace_root)
    resolved_target = os.path.abspath(os.path.join(resolved_root, relative_path))
    if (
        not resolved_target.startswith(resolved_root + os.sep)
        and resolved_target != resolved_root
    ):
        raise ValueError(
            f"Access Denied: Path '{relative_path}' is outside the workspace root."
        )
    return resolved_target


def _get_file_size(sandbox, workspace, path: str) -> int | None:
    """Returns the size of the file in bytes if it exists, otherwise None."""
    if not sandbox.use_docker:
        try:
            full_path = safe_resolve_path(workspace.repo_path, path)
            if os.path.isfile(full_path):
                return os.path.getsize(full_path)
        except Exception:
            return None
        return None

    # Docker mode: execute a Python script to get size
    b64_path = base64.b64encode(path.encode("utf-8")).decode("utf-8")
    py_code = (
        "import base64, os, sys\n"
        f"p = base64.b64decode('{b64_path}').decode('utf-8')\n"
        "if os.path.isfile(p):\n"
        "    print(os.path.getsize(p))\n"
        "else:\n"
        "    sys.exit(1)\n"
    )
    b64_py_code = base64.b64encode(py_code.encode("utf-8")).decode("utf-8")
    run_cmd = f"echo '{b64_py_code}' | base64 -d | python3"
    res = sandbox.exec_command(
        workspace.container_id, run_cmd, workdir=workspace.repo_path
    )
    if res.exit_code == 0:
        try:
            return int(res.stdout.strip())
        except ValueError:
            return None
    return None


def _check_destructive_overwrite(sandbox, workspace, path: str, content: str) -> None:
    """Rejects the write if the file already exists and the new content size is < 50%
    of the original size (with a 500-byte minimum to avoid false positives on tiny files).
    """
    old_size = _get_file_size(sandbox, workspace, path)
    if old_size is None:
        return

    new_size = len(content.encode("utf-8"))
    if old_size >= 500 and new_size < 0.5 * old_size:
        raise ValueError(
            f"Write rejected: The target file '{path}' already exists with size {old_size} bytes, "
            f"and the new content is only {new_size} bytes (a reduction of >50%). "
            "To prevent accidental truncation or content loss, destructive overwrites via file_write are blocked. "
            "Please use file_edit to modify specific sections of the file, or if you must overwrite, delete or truncate the file first."
        )


def file_read(sandbox, workspace, path: str) -> str:
    """Reads the contents of a file at the given relative path from the repository root, with line numbers."""
    if not sandbox.use_docker:
        full_path = safe_resolve_path(workspace.repo_path, path)
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            # Format with line numbers just like cat -n
            # Note: cat -n right-aligns line numbers in a 6-char block, followed by a tab or space.
            return "".join(f"{i:6d}\t{line}" for i, line in enumerate(lines, 1))
        except Exception as e:
            raise FileNotFoundError(f"Error reading file '{path}': {e}")

    # Docker mode
    b64_path = base64.b64encode(path.encode("utf-8")).decode("utf-8")
    py_code = (
        "import base64, sys\n"
        f"p = base64.b64decode('{b64_path}').decode('utf-8')\n"
        "try:\n"
        "    with open(p, 'r', encoding='utf-8', errors='ignore') as f:\n"
        "        for i, line in enumerate(f, 1):\n"
        "            sys.stdout.write(f'{i:6d}\\t{line}')\n"
        "except Exception as e:\n"
        "    sys.stderr.write(str(e))\n"
        "    sys.exit(1)\n"
    )
    b64_py_code = base64.b64encode(py_code.encode("utf-8")).decode("utf-8")
    run_cmd = f"echo '{b64_py_code}' | base64 -d | python3"

    res = sandbox.exec_command(
        workspace.container_id, run_cmd, workdir=workspace.repo_path
    )
    if res.exit_code != 0:
        raise FileNotFoundError(
            f"Error reading file '{path}': {res.stderr or res.stdout}"
        )
    return res.stdout


def file_write(sandbox, workspace, path: str, content: str) -> str:
    """Writes the entire content to a file. Creates directories if they do not exist."""
    # Guard against destructive overwrites: reject if new content is drastically
    # smaller than the existing file (likely the model hallucinated the full content).
    _check_destructive_overwrite(sandbox, workspace, path, content)

    if not sandbox.use_docker:
        full_path = safe_resolve_path(workspace.repo_path, path)
        try:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Successfully wrote file '{path}'"
        except Exception as e:
            raise RuntimeError(f"Error writing file '{path}': {e}")

    # Docker mode
    b64_path = base64.b64encode(path.encode("utf-8")).decode("utf-8")
    b64_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    py_code = (
        "import base64, os, sys\n"
        f"p = base64.b64decode('{b64_path}').decode('utf-8')\n"
        f"c = base64.b64decode('{b64_content}').decode('utf-8')\n"
        "try:\n"
        "    d = os.path.dirname(p)\n"
        "    if d:\n"
        "        os.makedirs(d, exist_ok=True)\n"
        "    with open(p, 'w', encoding='utf-8') as f:\n"
        "        f.write(c)\n"
        "except Exception as e:\n"
        "    sys.stderr.write(str(e))\n"
        "    sys.exit(1)\n"
    )
    b64_py_code = base64.b64encode(py_code.encode("utf-8")).decode("utf-8")
    run_cmd = f"echo '{b64_py_code}' | base64 -d | python3"

    res = sandbox.exec_command(
        workspace.container_id, run_cmd, workdir=workspace.repo_path
    )
    if res.exit_code != 0:
        raise RuntimeError(f"Error writing file '{path}': {res.stderr or res.stdout}")
    return f"Successfully wrote file '{path}'"


def file_edit(sandbox, workspace, path: str, search: str, replace: str) -> str:
    """Edits a file by replacing a unique search block with a replacement block. Only replaces the first occurrence."""
    if not sandbox.use_docker:
        full_path = safe_resolve_path(workspace.repo_path, path)
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content_str = f.read()
            if search not in content_str:
                raise ValueError("Search block not found in file.")
            if content_str.count(search) > 1:
                raise ValueError(
                    "Search block is not unique in file. Please provide more context."
                )

            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content_str.replace(search, replace, 1))
            return f"Successfully edited file '{path}'"
        except Exception as e:
            raise RuntimeError(f"Failed to edit file '{path}': {e}")

    # Docker mode
    b64_path = base64.b64encode(path.encode("utf-8")).decode("utf-8")
    b64_search = base64.b64encode(search.encode("utf-8")).decode("utf-8")
    b64_replace = base64.b64encode(replace.encode("utf-8")).decode("utf-8")

    py_code = (
        "import base64, sys\n"
        f"p = base64.b64decode('{b64_path}').decode('utf-8')\n"
        f"s = base64.b64decode('{b64_search}').decode('utf-8')\n"
        f"r = base64.b64decode('{b64_replace}').decode('utf-8')\n"
        "try:\n"
        "    with open(p, 'r', encoding='utf-8') as f: c = f.read()\n"
        "except Exception as e:\n"
        "    print(f'Error reading file: {e}')\n"
        "    sys.exit(1)\n"
        "if s not in c:\n"
        "    print('Error: Search block not found in file.')\n"
        "    sys.exit(2)\n"
        "if c.count(s) > 1:\n"
        "    print('Error: Search block is not unique in file. Please provide more context.')\n"
        "    sys.exit(3)\n"
        "with open(p, 'w', encoding='utf-8') as f: f.write(c.replace(s, r, 1))\n"
        "print('Success')\n"
    )

    b64_py_code = base64.b64encode(py_code.encode("utf-8")).decode("utf-8")
    run_cmd = f"echo '{b64_py_code}' | base64 -d | python3"

    res = sandbox.exec_command(
        workspace.container_id, run_cmd, workdir=workspace.repo_path
    )
    if res.exit_code != 0:
        error_msg = res.stdout.strip() or res.stderr.strip()
        raise RuntimeError(f"Failed to edit file '{path}': {error_msg}")

    return f"Successfully edited file '{path}'"


def file_search(sandbox, workspace, query: str, directory: str = ".") -> str:
    """Finds files matching the query pattern in the specified directory."""
    safe_resolve_path(workspace.repo_path, directory)

    if not sandbox.use_docker:
        search_root = safe_resolve_path(workspace.repo_path, directory)
        if not os.path.exists(search_root):
            raise FileNotFoundError(f"Directory '{directory}' not found.")

        matches = []
        for root, dirs, files in os.walk(search_root):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
            for file in files:
                if fnmatch.fnmatch(file, f"*{query}*"):
                    full_file_path = os.path.join(root, file)
                    # Get relative path to repository root
                    rel_path = os.path.relpath(full_file_path, workspace.repo_path)
                    matches.append(rel_path.replace("\\", "/"))
        return "\n".join(matches) or "No files found matching query."

    # Docker mode
    b64_query = base64.b64encode(query.encode("utf-8")).decode("utf-8")
    b64_dir = base64.b64encode(directory.encode("utf-8")).decode("utf-8")

    py_code = (
        "import base64, os, sys, fnmatch\n"
        f"q = base64.b64decode('{b64_query}').decode('utf-8')\n"
        f"d = base64.b64decode('{b64_dir}').decode('utf-8')\n"
        "if not os.path.exists(d):\n"
        "    print(f'Directory {d} not found')\n"
        "    sys.exit(1)\n"
        "matches = []\n"
        "for root, dirs, files in os.walk(d):\n"
        "    dirs[:] = [x for x in dirs if x not in ('.git', '__pycache__', 'node_modules', '.venv', 'build', 'dist')]\n"
        "    for file in files:\n"
        "        if fnmatch.fnmatch(file, f'*{q}*'):\n"
        "            full_path = os.path.join(root, file)\n"
        "            rel_path = os.path.relpath(full_path, '.')\n"
        "            matches.append(rel_path.replace('\\\\', '/'))\n"
        "if matches:\n"
        "    print('\\n'.join(matches))\n"
        "else:\n"
        "    print('No files found matching query.')\n"
    )
    b64_py_code = base64.b64encode(py_code.encode("utf-8")).decode("utf-8")
    run_cmd = f"echo '{b64_py_code}' | base64 -d | python3"
    res = sandbox.exec_command(
        workspace.container_id, run_cmd, workdir=workspace.repo_path
    )
    if res.exit_code != 0:
        raise RuntimeError(f"Error searching files: {res.stderr or res.stdout}")
    return res.stdout.strip()


def list_directory(sandbox, workspace, path: str = ".") -> str:
    """Lists the files and directories in the specified path (max depth 2, excludes hidden/build folders)."""
    safe_resolve_path(workspace.repo_path, path)

    if not sandbox.use_docker:
        search_root = safe_resolve_path(workspace.repo_path, path)
        if not os.path.exists(search_root):
            raise FileNotFoundError(f"Directory '{path}' not found.")

        results = []
        for root, dirs, files in os.walk(search_root):
            # Exclude hidden files, folders and __pycache__
            dirs[:] = [
                d for d in dirs if not d.startswith(".") and d not in EXCLUDED_DIRS
            ]

            # Compute relative path depth
            rel_root = os.path.relpath(root, search_root)
            depth = 0 if rel_root == "." else len(rel_root.split(os.sep))
            if depth >= 2:
                dirs[:] = []
                continue

            for d in dirs:
                d_rel = os.path.normpath(os.path.join(rel_root, d))
                results.append(os.path.join(path, d_rel))
            for f in files:
                if f.startswith("."):
                    continue
                f_rel = os.path.normpath(os.path.join(rel_root, f))
                results.append(os.path.join(path, f_rel))

        clean_results = []
        for r in results:
            cleaned = os.path.normpath(r).replace("\\", "/")
            if cleaned.startswith("./"):
                cleaned = cleaned[2:]
            if cleaned == ".":
                continue
            clean_results.append(cleaned)
        return "\n".join(sorted(clean_results)) or "Directory is empty."

    # Docker mode
    b64_path = base64.b64encode(path.encode("utf-8")).decode("utf-8")
    py_code = (
        "import base64, os, sys\n"
        f"p = base64.b64decode('{b64_path}').decode('utf-8')\n"
        "if not os.path.exists(p):\n"
        "    print(f'Directory {p} not found')\n"
        "    sys.exit(1)\n"
        "results = []\n"
        "for root, dirs, files in os.walk(p):\n"
        "    dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('__pycache__', 'node_modules', '.venv', 'build', 'dist')]\n"
        "    rel_root = os.path.relpath(root, p)\n"
        "    depth = 0 if rel_root == '.' else len(rel_root.split(os.sep))\n"
        "    if depth >= 2:\n"
        "        dirs[:] = []\n"
        "        continue\n"
        "    for d in dirs:\n"
        "        d_rel = os.path.normpath(os.path.join(rel_root, d))\n"
        "        results.append(os.path.normpath(os.path.join(p, d_rel)))\n"
        "    for f in files:\n"
        "        if f.startswith('.'):\n"
        "            continue\n"
        "        f_rel = os.path.normpath(os.path.join(rel_root, f))\n"
        "        results.append(os.path.normpath(os.path.join(p, f_rel)))\n"
        "clean_results = []\n"
        "for r in results:\n"
        "    cleaned = r.replace('\\\\', '/')\n"
        "    if cleaned.startswith('./'):\n"
        "        cleaned = cleaned[2:]\n"
        "    if cleaned == '.':\n"
        "        continue\n"
        "    clean_results.append(cleaned)\n"
        "if clean_results:\n"
        "    print('\\n'.join(sorted(clean_results)))\n"
        "else:\n"
        "    print('Directory is empty.')\n"
    )
    b64_py_code = base64.b64encode(py_code.encode("utf-8")).decode("utf-8")
    run_cmd = f"echo '{b64_py_code}' | base64 -d | python3"
    res = sandbox.exec_command(
        workspace.container_id, run_cmd, workdir=workspace.repo_path
    )
    if res.exit_code != 0:
        raise RuntimeError(f"Error listing directory: {res.stderr or res.stdout}")
    return res.stdout.strip()
