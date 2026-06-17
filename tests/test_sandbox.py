import os
import shutil
import tempfile
import subprocess
import pytest
from pathlib import Path

from autodev.sandbox.docker import SandboxManager
from autodev.sandbox.workspace import WorkspaceManager
from autodev.tools.toolkit import WorkspaceToolKit


@pytest.fixture
def mock_git_repo():
    """Fixture to create a local mock Git repository with simple code and requirements."""
    temp_dir = tempfile.mkdtemp()
    repo_path = Path(temp_dir)

    # Run git init
    subprocess.run(["git", "init", str(repo_path)], check=True, capture_output=True)

    # Create a simple python file
    py_file = repo_path / "main.py"
    py_file.write_text(
        "def hello():\n"
        "    print('Hello World')\n"
        "\n"
        "class MyClass:\n"
        "    def method(self):\n"
        "        pass\n",
        encoding="utf-8",
    )

    # Create a requirements.txt
    reqs = repo_path / "requirements.txt"
    reqs.write_text("pytest\n", encoding="utf-8")

    # Commit changes
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "."], cwd=str(repo_path), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )

    yield str(repo_path)

    # Cleanup repo
    try:

        def handle_remove_readonly(func, path, exc):
            import stat

            os.chmod(path, stat.S_IWRITE)
            func(path)

        shutil.rmtree(repo_path, onerror=handle_remove_readonly)
    except Exception:
        pass


def test_local_sandbox_flow(mock_git_repo):
    # 1. Initialize SandboxManager (forced local mode for testing or autodetected)
    sandbox = SandboxManager(allow_local_shell=True)
    # Force use_docker = False for testing local fallback behavior
    sandbox.use_docker = False

    workspace_mgr = WorkspaceManager(sandbox)
    workspace_id = "test-run"

    # 2. Setup workspace (clones our mock repo)
    workspace = workspace_mgr.setup_workspace(workspace_id, mock_git_repo)
    assert workspace.id == workspace_id
    assert os.path.exists(workspace.repo_path)
    assert os.path.exists(os.path.join(workspace.repo_path, "main.py"))

    # Verify Git exclusions file exists and contains __pycache__/
    exclude_path = os.path.join(workspace.repo_path, ".git", "info", "exclude")
    assert os.path.exists(exclude_path)
    with open(exclude_path, "r", encoding="utf-8") as f:
        exclude_content = f.read()
    assert "__pycache__/" in exclude_content

    # 3. Initialize toolkit
    toolkit = WorkspaceToolKit(sandbox, workspace)

    # 4. Test tools - file_read
    content = toolkit.file_read("main.py")
    assert "def hello():" in content
    assert "class MyClass:" in content

    # Test directory traversal protection (CWE-22)
    with pytest.raises(ValueError, match="outside the workspace root"):
        toolkit.file_read("../../../etc/passwd")

    # 5. Test tools - file_write (new file)
    write_res = toolkit.file_write("new_module.py", "def new_func():\n    return 42\n")
    assert "Successfully wrote file" in write_res
    assert os.path.exists(os.path.join(workspace.repo_path, "new_module.py"))

    # 6. Test tools - file_edit
    edit_res = toolkit.file_edit(
        path="main.py",
        search="    print('Hello World')",
        replace="    print('Hello Sandbox')\n    return True",
    )
    assert "Successfully edited file" in edit_res
    edited_content = toolkit.file_read("main.py")
    assert "print('Hello Sandbox')" in edited_content

    # 7. Test tools - grep_search
    grep_res = toolkit.grep_search("MyClass")
    assert "main.py:5:class MyClass:" in grep_res

    # 8. Test tools - git_status and git_diff
    status = toolkit.git_status()
    assert "main.py" in status or "new_module.py" in status
    diff = toolkit.git_diff()
    assert "print('Hello Sandbox')" in diff

    # 9. Test tools - index_codebase
    index_json = toolkit.index_codebase()
    import json

    index_data = json.loads(index_json)
    # The keys in the dict are file paths (possibly normalized)
    assert len(index_data) > 0
    main_key = [k for k in index_data.keys() if "main.py" in k][0]
    symbols = index_data[main_key]
    assert any(s["name"] == "hello" and s["type"] == "function" for s in symbols)
    assert any(s["name"] == "MyClass" and s["type"] == "class" for s in symbols)
    assert any(s["name"] == "method" and s["type"] == "method" for s in symbols)

    # 10. Test tools - shell_exec
    shell_res = toolkit.shell_exec("python3 main.py || python main.py")
    assert isinstance(shell_res, str)
    # Since we modified main.py (added return True, which might syntax error if outside function, but it's inside hello() so it's fine)
    # Wait, the command runs python on main.py, but it doesn't call hello(). It should not print anything unless we run a print.
    # 10.5. Test tools - file_write overwrite protection (size guard)
    # Write a file >= 500 chars
    large_content = "A" * 600
    toolkit.file_write("large_file.txt", large_content)
    # Attempting destructive overwrite (600 -> 100 bytes, < 50%) should fail
    with pytest.raises(ValueError, match="Write rejected"):
        toolkit.file_write("large_file.txt", "A" * 100)
    # Overwrite with >= 50% (600 -> 350 bytes) should succeed
    toolkit.file_write("large_file.txt", "A" * 350)

    # 11. Clean up workspace
    workspace_mgr.teardown_workspace(workspace)
    assert not os.path.exists(workspace.repo_path)
