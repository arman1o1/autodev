import base64
import json
import logging
import os
import ast
from autodev.tools.constants import EXCLUDED_DIRS

logger = logging.getLogger("autodev.tools.codebase")


def index_codebase(sandbox, workspace) -> str:
    """Indexes all Python files in the repository and returns a structured symbol table."""
    if not sandbox.use_docker:
        index = {}
        search_root = workspace.repo_path

        class SymbolExtractor(ast.NodeVisitor):
            def __init__(self, file_path):
                self.file_path = file_path
                self.symbols = []
                self.current_class = None

            def visit_ClassDef(self, node):
                self.symbols.append(
                    {
                        "name": node.name,
                        "type": "class",
                        "line": node.lineno,
                        "parent": self.current_class,
                    }
                )
                old_class = self.current_class
                self.current_class = node.name
                self.generic_visit(node)
                self.current_class = old_class

            def visit_FunctionDef(self, node):
                self.symbols.append(
                    {
                        "name": node.name,
                        "type": "method" if self.current_class else "function",
                        "line": node.lineno,
                        "parent": self.current_class,
                    }
                )
                self.generic_visit(node)

            def visit_AsyncFunctionDef(self, node):
                self.symbols.append(
                    {
                        "name": node.name,
                        "type": "method" if self.current_class else "function",
                        "line": node.lineno,
                        "parent": self.current_class,
                    }
                )
                self.generic_visit(node)

        for root, dirs, files in os.walk(search_root):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
            for file in files:
                if not file.endswith(".py"):
                    continue
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        tree = ast.parse(f.read())
                    rel_path = os.path.relpath(file_path, workspace.repo_path)
                    normalized_path = os.path.normpath(rel_path).replace("\\", "/")
                    extractor = SymbolExtractor(normalized_path)
                    extractor.visit(tree)
                    if extractor.symbols:
                        index[normalized_path] = extractor.symbols
                except Exception as e:
                    logger.warning(f"Failed to parse {file_path}: {e}")
        return json.dumps(index)

    # Docker mode
    py_code = (
        "import os, sys, ast, json\n"
        "class SymbolExtractor(ast.NodeVisitor):\n"
        "    def __init__(self, file_path):\n"
        "        self.file_path = file_path\n"
        "        self.symbols = []\n"
        "        self.current_class = None\n"
        "    def visit_ClassDef(self, node):\n"
        "        self.symbols.append({\n"
        "            'name': node.name,\n"
        "            'type': 'class',\n"
        "            'line': node.lineno,\n"
        "            'parent': self.current_class\n"
        "        })\n"
        "        old_class = self.current_class\n"
        "        self.current_class = node.name\n"
        "        self.generic_visit(node)\n"
        "        self.current_class = old_class\n"
        "    def visit_FunctionDef(self, node):\n"
        "        self.symbols.append({\n"
        "            'name': node.name,\n"
        "            'type': 'method' if self.current_class else 'function',\n"
        "            'line': node.lineno,\n"
        "            'parent': self.current_class\n"
        "        })\n"
        "        self.generic_visit(node)\n"
        "    def visit_AsyncFunctionDef(self, node):\n"
        "        self.symbols.append({\n"
        "            'name': node.name,\n"
        "            'type': 'method' if self.current_class else 'function',\n"
        "            'line': node.lineno,\n"
        "            'parent': self.current_class\n"
        "        })\n"
        "        self.generic_visit(node)\n"
        "\n"
        "index = {}\n"
        "for root, dirs, files in os.walk('.'):\n"
        "    dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', 'node_modules', '.venv', 'build', 'dist')]\n"
        "    for file in files:\n"
        "        if not file.endswith('.py'):\n"
        "            continue\n"
        "        file_path = os.path.join(root, file)\n"
        "        # Normalize file path representation\n"
        "        normalized_path = os.path.normpath(file_path)\n"
        "        try:\n"
        "            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:\n"
        "                tree = ast.parse(f.read())\n"
        "            extractor = SymbolExtractor(normalized_path)\n"
        "            extractor.visit(tree)\n"
        "            if extractor.symbols:\n"
        "                index[normalized_path] = extractor.symbols\n"
        "        except Exception:\n"
        "            pass\n"
        "print(json.dumps(index))\n"
    )

    b64_py_code = base64.b64encode(py_code.encode("utf-8")).decode("utf-8")
    run_cmd = f"echo '{b64_py_code}' | base64 -d | python3"

    res = sandbox.exec_command(
        workspace.container_id, run_cmd, workdir=workspace.repo_path
    )
    if res.exit_code != 0:
        raise RuntimeError(
            f"Error during codebase indexing: {res.stderr or res.stdout}"
        )

    return res.stdout.strip()
