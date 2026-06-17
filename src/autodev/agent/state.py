from typing import Dict, List, Optional
from pydantic import BaseModel, Field
from autodev.agent.modes import Mode


class TestResult(BaseModel):
    command: str
    passed: bool
    stdout: str
    stderr: str
    exit_code: int


class EditRecord(BaseModel):
    search: str
    replace: str
    timestamp: float


class AgentState(BaseModel):
    # Issue context
    issue_url: str
    issue_title: str = ""
    issue_body: str = ""
    repo_url: str = ""

    # Codebase understanding
    codebase_summary: str = ""

    # Planning
    plan: Optional[str] = None
    plan_approved: bool = False

    # Execution tracking
    current_mode: Mode = Mode.PLANNING
    files_modified: Dict[str, List[EditRecord]] = Field(default_factory=dict)
    attempt_number: int = 1
    max_attempts: int = 3

    # Testing & Debugging
    test_command: Optional[str] = None
    test_history: List[TestResult] = Field(default_factory=list)
    errors_encountered: List[str] = Field(default_factory=list)

    # Final outputs
    pr_url: Optional[str] = None

    @property
    def is_terminal(self) -> bool:
        return self.current_mode in (Mode.DONE, Mode.FAILED)

    @property
    def last_test_passed(self) -> bool:
        if not self.test_history:
            return False
        return self.test_history[-1].passed

    @property
    def last_test_output(self) -> str:
        if not self.test_history:
            return "No tests executed yet."
        last = self.test_history[-1]
        out = ""
        if last.stdout:
            out += f"STDOUT:\n{last.stdout}\n"
        if last.stderr:
            out += f"STDERR:\n{last.stderr}\n"
        return out

    def to_formatted_summary(self) -> str:
        """Generates a text summary of the agent state for LLM prompts."""
        files_mod_list = list(self.files_modified.keys())
        summary = (
            f"--- CURRENT AGENT STATE ---\n"
            f"Current Mode: {self.current_mode.value}\n"
            f"Attempt: {self.attempt_number} of {self.max_attempts}\n"
            f"Files Modified: {', '.join(files_mod_list) if files_mod_list else 'None'}\n"
        )
        if self.test_command:
            summary += f"\nTest Command: {self.test_command}\n"
        if self.test_history:
            last_test = self.test_history[-1]
            summary += f"\nLast Test Status: {'PASSED' if last_test.passed else 'FAILED'} (Exit Code: {last_test.exit_code})\n"
        return summary

    def add_error(self, error: str, max_history: int = 10) -> None:
        """Appends an error and trims history to the last max_history entries."""
        self.errors_encountered.append(error)
        if len(self.errors_encountered) > max_history:
            self.errors_encountered = self.errors_encountered[-max_history:]

    @property
    def interactive(self) -> bool:
        """Provided for backwards compatibility — check settings.interactive instead."""
        return False
