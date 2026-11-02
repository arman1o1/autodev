import base64
import json
import logging
import os
import ast
from typing import List, Dict, Any, Optional
from autodev.tools.constants import is_excluded_dir

try:
    from tree_sitter import Language, Parser
    import tree_sitter_python

    _PY_LANG = Language(tree_sitter_python.language())
    _PARSER = Parser(_PY_LANG)
    HAS_TREE_SITTER = True
except Exception:
    HAS_TREE_SITTER = False

logger = logging.getLogger("autodev.tools.codebase")


def _extract_symbols_tree_sitter(code_bytes: bytes) -> List[Dict[str, Any]]:
    """Extracts symbols using tree-sitter AST parser."""
    tree = _PARSER.parse(code_bytes)

    def _traverse(node, parent_class: Optional[str] = None) -> List[Dict[str, Any]]:
        symbols: List[Dict[str, Any]] = []
        for child in node.children:
            if child.type == "class_definition":
                name_node = child.child_by_field_name("name")
                name = (
                    name_node.text.decode("utf-8", errors="replace")
                    if name_node
                    else "AnonymousClass"
                )
                symbols.append(
                    {
                        "name": name,
                        "type": "class",
                        "line": child.start_point.row + 1,
                        "parent": parent_class,
                    }
                )
                body_node = child.child_by_field_name("body")
                if body_node:
                    symbols.extend(_traverse(body_node, parent_class=name))
            elif child.type in ("function_definition", "async_function_definition"):
                name_node = child.child_by_field_name("name")
                name = (
                    name_node.text.decode("utf-8", errors="replace")
                    if name_node
                    else "anonymous"
                )
                symbols.append(
                    {
                        "name": name,
                        "type": "method" if parent_class else "function",
                        "line": child.start_point.row + 1,
                        "parent": parent_class,
                    }
                )
                body_node = child.child_by_field_name("body")
                if body_node:
                    symbols.extend(_traverse(body_node, parent_class=parent_class))
        return symbols

    return _traverse(tree.root_node)


def _extract_symbols_ast(source_text: str) -> List[Dict[str, Any]]:
    """Fallback symbol extraction using standard Python ast module."""

    class AstSymbolExtractor(ast.NodeVisitor):
        def __init__(self):
            self.symbols: List[Dict[str, Any]] = []
            self.current_class: Optional[str] = None

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

    tree = ast.parse(source_text)
    extractor = AstSymbolExtractor()
    extractor.visit(tree)
    return extractor.symbols


def index_codebase(sandbox, workspace) -> str:
    """Indexes all Python files in the repository and returns a structured symbol table."""
    if not sandbox.use_docker:
        index = {}
        search_root = workspace.repo_path

        for root, dirs, files in os.walk(search_root):
            dirs[:] = [d for d in dirs if not is_excluded_dir(d)]
            for file in files:
                if not file.endswith(".py"):
                    continue
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, "rb") as f:
                        code_bytes = f.read()

                    symbols = []
                    if HAS_TREE_SITTER:
                        try:
                            symbols = _extract_symbols_tree_sitter(code_bytes)
                        except Exception as ts_err:
                            logger.debug(
                                f"Tree-sitter failed on {file_path}, falling back to ast: {ts_err}"
                            )
                            source_text = code_bytes.decode("utf-8", errors="ignore")
                            symbols = _extract_symbols_ast(source_text)
                    else:
                        source_text = code_bytes.decode("utf-8", errors="ignore")
                        symbols = _extract_symbols_ast(source_text)

                    rel_path = os.path.relpath(file_path, workspace.repo_path)
                    normalized_path = os.path.normpath(rel_path).replace("\\", "/")
                    if symbols:
                        index[normalized_path] = symbols
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
        "    dirs[:] = [d for d in dirs if not d.startswith('.') and not d.endswith('.egg-info') and d not in ('__pycache__', 'node_modules', '.venv', 'build', 'dist')]\n"
        "    for file in files:\n"
        "        if not file.endswith('.py'):\n"
        "            continue\n"
        "        file_path = os.path.join(root, file)\n"
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
