import tempfile
import os
import shutil
from pathlib import Path
from autodev.tools.constants import is_excluded_dir
from autodev.tools.toolkit import WorkspaceToolKit
from autodev.sandbox.docker import SandboxManager
from autodev.sandbox.workspace import Workspace
from autodev.agent.context import ContextManager
from autodev.agent.state import AgentState
from autodev.agent.modes import Mode


def test_is_excluded_dir():
    assert is_excluded_dir(".git") is True
    assert is_excluded_dir("__pycache__") is True
    assert is_excluded_dir("mypackage.egg-info") is True
    assert is_excluded_dir("other.dist-info") is True
    assert is_excluded_dir("src") is False
    assert is_excluded_dir("tests") is False


def test_file_read_line_range():
    temp_dir = tempfile.mkdtemp()
    try:
        sample_path = os.path.join(temp_dir, "sample.txt")
        lines = [f"Line {i}\n" for i in range(1, 21)]
        with open(sample_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        sandbox = SandboxManager(allow_local_shell=True)
        sandbox.use_docker = False
        workspace = Workspace(
            id="test-range", repo_url="", container_id="local", repo_path=temp_dir
        )
        toolkit = WorkspaceToolKit(sandbox, workspace)

        # Read lines 5 to 8
        content = toolkit.file_read("sample.txt", start_line=5, end_line=8)
        assert "Line 5" in content
        assert "Line 8" in content
        assert "Line 4" not in content
        assert "Line 9" not in content
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_declare_transition_tool():
    sandbox = SandboxManager(allow_local_shell=True)
    sandbox.use_docker = False
    workspace = Workspace(
        id="test-trans", repo_url="", container_id="local", repo_path="."
    )
    toolkit = WorkspaceToolKit(sandbox, workspace)
    res = toolkit.declare_transition(target_mode="CODING", summary="Plan is ready.")
    assert "TRANSITION_REQUESTED:CODING:Plan is ready." in res


def test_context_manager_and_token_accounting():
    temp_dir = tempfile.mkdtemp()
    try:
        mgr = ContextManager(
            workspace_id="test-run-123",
            log_dir=Path(temp_dir),
            max_tokens=1000,
            save_run_logs=True,
        )

        mgr.record_usage(prompt_tokens=1000, completion_tokens=500)
        assert mgr.total_prompt_tokens == 1000
        assert mgr.total_completion_tokens == 500
        assert mgr.estimated_cost_usd > 0.0

        # Test reset for mode
        mgr.reset_for_mode("Initial planning prompt")
        assert len(mgr.messages) == 1

        # Test transcript persistence
        transcript_file = Path(temp_dir) / "runs" / "test-run-123" / "transcript.jsonl"
        assert transcript_file.exists()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_agent_state_fields():
    state = AgentState(
        issue_url="https://github.com/foo/bar/issues/1",
        interactive=True,
        max_attempts=5,
    )
    assert state.interactive is True
    assert state.max_attempts == 5
    assert state.current_mode == Mode.PLANNING
    assert state.total_prompt_tokens == 0
    assert state.estimated_cost_usd == 0.0
