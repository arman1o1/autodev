import os
import json
import shutil
import tempfile
import pytest
from unittest.mock import MagicMock, patch

from autodev.config import AutodevSettings
from autodev.evaluation.benchmark import BenchmarkRunner
from autodev.agent.core import AutodevAgent
from autodev.agent.modes import Mode
from autodev.agent.state import AgentState


@pytest.fixture
def mock_benchmark_file():
    """Fixture to create a temporary JSON benchmark configuration."""
    temp_dir = tempfile.mkdtemp()
    file_path = os.path.join(temp_dir, "test_bench.json")

    mock_issues = [
        {
            "issue_url": "https://github.com/test-owner/test-repo/issues/1",
            "issue_title": "Fix bug 1",
            "issue_body": "Describe bug 1",
            "repo_url": "https://github.com/test-owner/test-repo.git",
        },
        {
            "issue_url": "https://github.com/test-owner/test-repo/issues/2",
            "issue_title": "Fix bug 2",
            "issue_body": "Describe bug 2",
            "repo_url": "https://github.com/test-owner/test-repo.git",
        },
    ]

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(mock_issues, f)

    yield file_path, temp_dir

    # Cleanup
    shutil.rmtree(temp_dir, ignore_errors=True)


@patch("autodev.evaluation.benchmark.AutodevAgent")
@patch("autodev.evaluation.benchmark.GeminiClient")
@patch("autodev.evaluation.benchmark.SandboxManager")
def test_benchmark_runner_flow(
    mock_sandbox, mock_gemini, mock_agent_class, mock_benchmark_file
):
    bench_file, temp_dir = mock_benchmark_file

    # 1. Setup mock AgentState responses
    # Issue 1 returns success (DONE)
    state1 = MagicMock(spec=AgentState)
    state1.current_mode = Mode.DONE
    state1.attempt_number = 1
    state1.files_modified = {"main.py": []}
    state1.errors_encountered = []

    # Issue 2 returns failure (FAILED)
    state2 = MagicMock(spec=AgentState)
    state2.current_mode = Mode.FAILED
    state2.attempt_number = 3
    state2.files_modified = {}
    state2.errors_encountered = ["Compile error"]

    # Mock agent instance
    mock_agent_instance = MagicMock(spec=AutodevAgent)
    mock_agent_instance.solve.side_effect = [state1, state2]
    mock_agent_class.return_value = mock_agent_instance

    # 2. Initialize BenchmarkRunner
    settings = AutodevSettings(
        gemini_api_key="mock-key", gemini_model="gemini-3.5-flash"
    )
    runner = BenchmarkRunner(settings)

    # 3. Run benchmark
    results_dir = os.path.join(temp_dir, "results")
    summary = runner.run_benchmark(bench_file, output_dir=results_dir)

    # 4. Assertions on summary
    assert summary["total_issues"] == 2
    assert summary["successes"] == 1
    assert summary["failures"] == 1
    assert summary["success_rate_percent"] == 50.0

    # Assert report file exists
    report_files = os.listdir(results_dir)
    assert len(report_files) == 1
    assert report_files[0].startswith("report-test_bench")

    # Verify content of saved report
    report_path = os.path.join(results_dir, report_files[0])
    with open(report_path, "r", encoding="utf-8") as f:
        saved_summary = json.load(f)

    assert saved_summary["successes"] == 1
    assert saved_summary["results"][0]["success"] is True
    assert saved_summary["results"][1]["success"] is False
    assert saved_summary["results"][1]["error"] == "Compile error"
