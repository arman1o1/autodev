import base64
import logging
import os
import re
from autodev.tools.file_ops import safe_resolve_path
from autodev.tools.constants import EXCLUDED_DIRS

logger = logging.getLogger("autodev.tools.grep")


def grep_search(
    sandbox, workspace, pattern: str, path: str = ".", case_sensitive: bool = True
) -> str:
    """Searches for a pattern in all files under the specified path.

    Returns matching filenames, line numbers, and line contents.
    """
    safe_resolve_path(workspace.repo_path, path)

    if not sandbox.use_docker:
        search_root = os.path.join(workspace.repo_path, path)
        if not os.path.exists(search_root):
            raise FileNotFoundError(f"Path '{path}' not found.")

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(re.escape(pattern), flags)
        except Exception as e:
            raise ValueError(f"Invalid search pattern: {e}")

        matches = []
        for root, dirs, files in os.walk(search_root):
            # Exclude common directories
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
            for file in files:
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        for i, line in enumerate(f, 1):
                            if regex.search(line):
                                rel_path = os.path.relpath(
                                    file_path, workspace.repo_path
                                )
                                rel_path_unix = rel_path.replace("\\", "/")
                                matches.append(f"{rel_path_unix}:{i}:{line.strip()}")
                except Exception:
                    pass
        if matches:
            total = len(matches)
            output = "\n".join(matches[:200])
            if total > 200:
                output += f"\n\n[Showing 200 of {total} matches. Refine your search pattern for more specific results.]"
            return output
        else:
            return "No matches found."

    # Docker mode
    b64_pattern = base64.b64encode(pattern.encode("utf-8")).decode("utf-8")
    b64_path = base64.b64encode(path.encode("utf-8")).decode("utf-8")

    # Python script to run inside sandbox
    py_code = (
        "import os, sys, base64, re\n"
        f"root_path = base64.b64decode('{b64_path}').decode('utf-8')\n"
        f"pattern = base64.b64decode('{b64_pattern}').decode('utf-8')\n"
        f"case_sensitive = {case_sensitive}\n"
        "flags = 0 if case_sensitive else re.IGNORECASE\n"
        "try:\n"
        "    regex = re.compile(re.escape(pattern), flags)\n"
        "except Exception as e:\n"
        "    print(f'Invalid search pattern: {e}')\n"
        "    sys.exit(1)\n"
        "matches = []\n"
        "for root, dirs, files in os.walk(root_path):\n"
        "    # Exclude common directories\n"
        "    dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', 'node_modules', '.venv', 'build', 'dist')]\n"
        "    for file in files:\n"
        "        file_path = os.path.join(root, file)\n"
        "        try:\n"
        "            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:\n"
        "                for i, line in enumerate(f, 1):\n"
        "                    if regex.search(line):\n"
        "                        rel_path = os.path.relpath(file_path, '.')\n"
        "                        rel_path_unix = rel_path.replace('\\\\', '/')\n"
        "                        matches.append(f'{rel_path_unix}:{i}:{line.strip()}')\n"
        "        except Exception:\n"
        "            pass\n"
        "if matches:\n"
        "    print('\\n'.join(matches[:200])) # Limit to first 200 matches\n"
        "else:\n"
        "    print('No matches found.')\n"
    )

    b64_py_code = base64.b64encode(py_code.encode("utf-8")).decode("utf-8")
    run_cmd = f"echo '{b64_py_code}' | base64 -d | python3"

    res = sandbox.exec_command(
        workspace.container_id, run_cmd, workdir=workspace.repo_path
    )
    if res.exit_code != 0:
        raise RuntimeError(f"Error during grep search: {res.stderr or res.stdout}")

    return res.stdout.strip()
