import os
import shutil
import tempfile
import subprocess
import pytest
from unittest.mock import MagicMock

from autodev.config import AutodevSettings
from autodev.sandbox.docker import SandboxManager
from autodev.agent.core import AutodevAgent
from autodev.agent.modes import Mode
from autodev.llm.client import GeminiClient, LLMResponse


@pytest.fixture
def mock_git_repo():
    """Fixture to create a local mock Git repository with simple code."""
    temp_dir = tempfile.mkdtemp()
    repo_path = temp_dir

    # Run git init
    subprocess.run(["git", "init", repo_path], check=True, capture_output=True)

    # Create a simple python file
    py_file = os.path.join(repo_path, "main.py")
    with open(py_file, "w") as f:
        f.write("def hello():\n    print('Hello World')\n")

    # Commit changes
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    yield repo_path

    # Cleanup repo
    try:

        def handle_remove_readonly(func, path, exc):
            import stat

            os.chmod(path, stat.S_IWRITE)
            func(path)

        shutil.rmtree(repo_path, onerror=handle_remove_readonly)
    except Exception:
        pass


def test_agent_react_loop_flow(mock_git_repo):
    # 1. Setup config
    settings = AutodevSettings(
        gemini_api_key="mock-key", gemini_model="gemini-3.5-flash", max_retries=3
    )

    # 2. Mock GeminiClient
    mock_llm = MagicMock(spec=GeminiClient)
    mock_llm.model_name = "gemini-3.5-flash"

    # Define sequence of LLM responses to drive the state machine
    responses = [
        # Turn 1: PLANNING - Call list_directory to see files
        LLMResponse(
            text="I will check the files first.",
            tool_calls=[{"name": "list_directory", "args": {"path": "."}}],
            model_name="gemini-3.5-flash",
        ),
        # Turn 2: PLANNING - Return the plan (text only)
        LLMResponse(
            text="### IMPLEMENTATION PLAN\n1. Modify main.py to say Sandbox.",
            tool_calls=[],
            model_name="gemini-3.5-flash",
        ),
        # Turn 3: CODING - Read main.py
        LLMResponse(
            text="I will read main.py.",
            tool_calls=[{"name": "file_read", "args": {"path": "main.py"}}],
            model_name="gemini-3.5-flash",
        ),
        # Turn 4: CODING - Modify main.py
        LLMResponse(
            text="I will modify main.py.",
            tool_calls=[
                {
                    "name": "file_edit",
                    "args": {
                        "path": "main.py",
                        "search": "    print('Hello World')",
                        "replace": "    print('Hello Sandbox')",
                    },
                }
            ],
            model_name="gemini-3.5-flash",
        ),
        # Turn 5: CODING - Declare coding complete
        LLMResponse(
            text="Coding is complete. Ready for testing.",
            tool_calls=[],
            model_name="gemini-3.5-flash",
        ),
        # Turn 6: TESTING - Execute test run
        LLMResponse(
            text="I will run the tests.",
            tool_calls=[
                {"name": "shell_exec", "args": {"command": "echo 'test passed'"}}
            ],
            model_name="gemini-3.5-flash",
        ),
        # Turn 7: TESTING - Declare testing complete (last test mock passed)
        LLMResponse(
            text="Tests have passed. All changes verified.",
            tool_calls=[],
            model_name="gemini-3.5-flash",
        ),
    ]
    mock_llm.generate.side_effect = responses

    # 3. Setup sandbox
    sandbox = SandboxManager(allow_local_shell=True)
    sandbox.use_docker = False  # Force local mode

    # 4. Instantiate Agent
    agent = AutodevAgent(settings, mock_llm, sandbox)

    # 5. Run agent on mock repo
    final_state = agent.solve(
        issue_url="https://github.com/test-owner/test-repo/issues/10",
        repo_url=mock_git_repo,
    )

    # 6. Verify state transitions
    assert final_state.current_mode == Mode.DONE
    assert final_state.plan_approved is True
    assert "main.py" in final_state.files_modified
    assert len(final_state.test_history) == 1
    assert final_state.test_history[0].passed is True
    assert final_state.attempt_number == 1
