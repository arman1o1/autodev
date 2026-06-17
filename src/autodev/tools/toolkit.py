import logging
from autodev.sandbox.docker import SandboxManager
from autodev.sandbox.workspace import Workspace
from autodev.tools import file_ops, shell, grep, git_ops, codebase

logger = logging.getLogger("autodev.tools.toolkit")


class WorkspaceToolKit:
    """A collection of tools that can be executed in the sandboxed workspace.

    All file paths are relative to the repository root.
    """

    def __init__(self, sandbox: SandboxManager, workspace: Workspace):
        self.sandbox = sandbox
        self.workspace = workspace

    def file_read(self, path: str) -> str:
        """Reads the contents of a file at the given relative path from the repository root, with line numbers.

        Args:
            path: Relative path to the file (e.g. 'src/main.py').
        """
        return file_ops.file_read(self.sandbox, self.workspace, path)

    def file_write(self, path: str, content: str) -> str:
        """Writes the entire content to a file at the given relative path. Creates directories if they do not exist.

        Args:
            path: Relative path to write the file (e.g. 'tests/test_foo.py').
            content: The text content to write to the file.
        """
        return file_ops.file_write(self.sandbox, self.workspace, path, content)

    def file_edit(self, path: str, search: str, replace: str) -> str:
        """Edits a file by replacing a unique search block with a replacement block.

        Args:
            path: Relative path to the file.
            search: The exact block of text to search for (must be unique).
            replace: The block of text to replace it with.
        """
        return file_ops.file_edit(self.sandbox, self.workspace, path, search, replace)

    def file_search(self, query: str, directory: str = ".") -> str:
        """Finds files matching the query pattern (glob) in the specified directory.

        Args:
            query: String pattern to search in filenames (e.g. 'test_cli').
            directory: Directory to search inside (default is root '.').
        """
        return file_ops.file_search(self.sandbox, self.workspace, query, directory)

    def list_directory(self, path: str = ".") -> str:
        """Lists files and directories in the specified path (max depth 2, excludes hidden files).

        Args:
            path: Path to list (default is root '.').
        """
        return file_ops.list_directory(self.sandbox, self.workspace, path)

    def shell_exec(self, command: str, timeout: int = 120) -> str:
        """Executes a shell command inside the sandboxed repository workspace.

        Use this to run tests (e.g. 'pytest'), install temporary tools, or run scripts.
        Command runs in a non-interactive bash shell.

        Args:
            command: The command line string to run (e.g. 'pytest tests/test_parser.py').
            timeout: Maximum execution time in seconds (default is 120).
        """
        return shell.shell_exec(self.sandbox, self.workspace, command, timeout)

    def grep_search(
        self, pattern: str, path: str = ".", case_sensitive: bool = True
    ) -> str:
        """Searches for a pattern recursively in file contents under the specified path.

        Args:
            pattern: The text pattern to search for (literal match, not regex).
            path: Root directory to search from (default is '.').
            case_sensitive: Whether the search should match case exactly (default True).
        """
        return grep.grep_search(
            self.sandbox, self.workspace, pattern, path, case_sensitive
        )

    def git_diff(self) -> str:
        """Returns the current unstaged git changes in the repository."""
        return git_ops.git_diff(self.sandbox, self.workspace)

    def git_status(self) -> str:
        """Returns the current git status of the repository workspace."""
        return git_ops.git_status(self.sandbox, self.workspace)

    def git_log(self, limit: int = 5) -> str:
        """Returns the last few commit messages in the current branch.

        Args:
            limit: Number of commit messages to show (default 5).
        """
        return git_ops.git_log(self.sandbox, self.workspace, limit)

    def index_codebase(self) -> str:
        """Indexes all Python files in the repository and returns a JSON string mapping files to their AST symbols."""
        return codebase.index_codebase(self.sandbox, self.workspace)
