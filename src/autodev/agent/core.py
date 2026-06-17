import logging
import time
import re
import hashlib
import json
import difflib
from collections import deque
from typing import List, Dict, Any, Optional
from google.genai import types
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

from autodev.config import AutodevSettings
from autodev.llm.client import GeminiClient
from autodev.sandbox.docker import SandboxManager
from autodev.sandbox.workspace import WorkspaceManager
from autodev.tools.toolkit import WorkspaceToolKit
from autodev.tools.registry import ToolRegistry, ToolResult
from autodev.agent.modes import Mode
from autodev.agent.state import AgentState, TestResult, EditRecord
from autodev.agent import prompts
from autodev.github.issues import IssueFetcher, parse_issue_url
from autodev.github.pull_requests import PRCreator

logger = logging.getLogger("autodev.agent")
console = Console()


class AutodevAgent:
    def __init__(
        self,
        settings: AutodevSettings,
        llm_client: GeminiClient,
        sandbox_mgr: SandboxManager,
    ):
        self.settings = settings
        self.llm = llm_client
        self.sandbox = sandbox_mgr
        self.workspace_mgr = WorkspaceManager(sandbox_mgr)

    def _get_tools_for_mode(self, toolkit: WorkspaceToolKit, mode: Mode) -> List[Any]:
        """Returns the subset of tools allowed for the given agent execution mode."""
        if mode == Mode.PLANNING:
            return [
                toolkit.index_codebase,
                toolkit.list_directory,
                toolkit.file_search,
                toolkit.grep_search,
                toolkit.file_read,
            ]
        elif mode == Mode.CODING:
            return [
                toolkit.file_read,
                toolkit.file_write,
                toolkit.file_edit,
                toolkit.git_diff,
                toolkit.git_status,
            ]
        elif mode == Mode.TESTING:
            return [toolkit.shell_exec, toolkit.file_read, toolkit.list_directory]
        elif mode == Mode.DEBUGGING:
            return [
                toolkit.file_read,
                toolkit.file_edit,
                toolkit.shell_exec,
                toolkit.git_diff,
                toolkit.grep_search,
            ]
        return []

    def _get_system_prompt_for_mode(self, mode: Mode) -> str:
        if mode == Mode.PLANNING:
            return prompts.PLANNING_SYSTEM_PROMPT
        elif mode == Mode.CODING:
            return prompts.CODING_SYSTEM_PROMPT
        elif mode == Mode.TESTING:
            return prompts.TESTING_SYSTEM_PROMPT
        elif mode == Mode.DEBUGGING:
            return prompts.DEBUGGING_SYSTEM_PROMPT
        return ""

    def solve(
        self,
        issue_url: str,
        issue_title: str = "Test Issue",
        issue_body: str = "Please resolve the bug.",
        repo_url: Optional[str] = None,
    ) -> AgentState:
        """Runs the complete plan -> code -> test -> debug loop end-to-end."""
        owner, repo, number = None, None, None
        # 1. Parse issue URL and fetch metadata from GitHub
        try:
            owner, repo, number = parse_issue_url(issue_url)
            fetcher = IssueFetcher(self.settings.github_token)
            github_issue = fetcher.fetch_issue(issue_url)

            issue_title = github_issue.title
            issue_body = github_issue.body
            repo_url = github_issue.repo_url
            logger.info(
                f"Successfully fetched issue details for #{number}: {issue_title}"
            )
        except Exception as e:
            logger.warning(
                f"Failed to fetch issue details from GitHub. Using fallbacks or arguments. Error: {e}"
            )
            if not repo_url:
                parts = issue_url.rstrip("/").split("/")
                if len(parts) >= 5 and "github.com" in parts[2]:
                    owner = parts[3]
                    repo = parts[4]
                    repo_url = f"https://github.com/{owner}/{repo}.git"
                else:
                    raise ValueError(
                        f"Could not parse repository URL from issue: {issue_url}. Please provide repo_url explicitly."
                    )
            owner = owner if owner is not None else "owner"
            repo = repo if repo is not None else "repo"
            number = number if number is not None else 1

        branch_name = f"autodev/issue-{number}"

        # 2. Setup state
        workspace_id = f"run-{int(time.time())}"
        state = AgentState(
            issue_url=issue_url,
            issue_title=issue_title,
            issue_body=issue_body,
            repo_url=repo_url,
            max_attempts=self.settings.max_retries,
            interactive=self.settings.interactive,
        )

        console.print("[bold green]Setting up workspace...[/bold green]")
        workspace = self.workspace_mgr.setup_workspace(
            workspace_id, repo_url, branch_name=branch_name
        )

        try:
            # 3. Setup toolkit and registry
            toolkit = WorkspaceToolKit(self.sandbox, workspace)
            registry = ToolRegistry()
            registry.register_all_from_class(toolkit)

            # 4. Generate initial codebase index
            console.print("[bold green]Indexing codebase structure...[/bold green]")
            try:
                state.codebase_summary = toolkit.index_codebase()
            except Exception as e:
                logger.warning(f"Failed to generate codebase summary: {e}")
                state.codebase_summary = "{}"

            # 5. Initialize conversation history
            messages: List[types.Content] = []

            initialized_mode: Optional[Mode] = None
            initialized_attempt: int = 1
            consecutive_identical_responses = 0
            last_text_response = ""
            tests_at_attempt_start = 0
            force_tool_next_turn = False
            loop_iteration = 0

            # Bug #1: Tool-call dedup / perseveration detection
            recent_tool_signatures: deque = deque(maxlen=20)
            tool_stall_warnings = 0

            # Bug #2: Per-mode step budget
            steps_in_current_mode = 0
            MODE_STEP_BUDGETS = {
                Mode.PLANNING: self.settings.max_steps_per_mode,
                Mode.CODING: int(self.settings.max_steps_per_mode * 1.25),
                Mode.TESTING: int(self.settings.max_steps_per_mode * 0.5),
                Mode.DEBUGGING: int(self.settings.max_steps_per_mode * 0.75),
            }

            # 6. ReAct Loop
            while not state.is_terminal:
                loop_iteration += 1
                if loop_iteration > self.settings.max_loop_iterations:
                    logger.warning(
                        f"Max loop iterations ({self.settings.max_loop_iterations}) reached. Failing."
                    )
                    state.current_mode = Mode.FAILED
                    state.add_error(
                        f"Max loop iterations ({self.settings.max_loop_iterations}) exceeded."
                    )
                    break

                # Bug #2: Per-mode step budget check
                steps_in_current_mode += 1
                mode_budget = MODE_STEP_BUDGETS.get(
                    state.current_mode, self.settings.max_steps_per_mode
                )
                if steps_in_current_mode > mode_budget:
                    logger.warning(
                        f"Mode {state.current_mode.value} exceeded step budget ({mode_budget})."
                    )
                    state.add_error(
                        f"Mode {state.current_mode.value} exceeded its step budget of {mode_budget} iterations."
                    )
                    state.current_mode = Mode.FAILED
                    break
                # If the mode or attempt changed, re-initialize the conversation history for the current mode/attempt!
                if (
                    state.current_mode != initialized_mode
                    or state.attempt_number != initialized_attempt
                ):
                    steps_in_current_mode = (
                        0  # Reset per-mode counter on mode/attempt change
                    )
                    messages = []
                    if state.current_mode == Mode.PLANNING:
                        initial_prompt = (
                            f"GitHub Issue: {state.issue_title}\n"
                            f"Description:\n{state.issue_body}\n\n"
                            f"Initial Codebase Symbols:\n{state.codebase_summary}\n\n"
                            f"Please begin by exploring the codebase to understand the problem."
                        )
                        messages.append(
                            types.Content(
                                role="user",
                                parts=[types.Part.from_text(text=initial_prompt)],
                            )
                        )
                    elif state.current_mode == Mode.CODING:
                        coding_prompt = (
                            f"GitHub Issue: {state.issue_title}\n"
                            f"Description:\n{state.issue_body}\n\n"
                            f"Approved Implementation Plan:\n{state.plan}\n\n"
                            f"Please execute the implementation plan. You must use the writing/editing tools (like `file_write` or `file_edit`) to modify the files as described in the plan.\n"
                            f"Remember, no changes have been made to the codebase yet. You must apply them now."
                        )
                        messages.append(
                            types.Content(
                                role="user",
                                parts=[types.Part.from_text(text=coding_prompt)],
                            )
                        )
                    elif state.current_mode == Mode.TESTING:
                        testing_prompt = (
                            f"GitHub Issue: {state.issue_title}\n"
                            f"Description:\n{state.issue_body}\n\n"
                            f"Approved Implementation Plan:\n{state.plan}\n\n"
                            f"The coding phase is complete. Here are the modified files:\n"
                            f"{', '.join(state.files_modified.keys()) if state.files_modified else 'None'}\n\n"
                            f"Please identify the test suite and run the tests using `shell_exec` (e.g. `pytest`, `python -m unittest`, or the appropriate test runner).\n"
                            f"You MUST run the tests to verify the changes. Do not just read test files or declare success without executing them."
                        )
                        messages.append(
                            types.Content(
                                role="user",
                                parts=[types.Part.from_text(text=testing_prompt)],
                            )
                        )
                    elif state.current_mode == Mode.DEBUGGING:
                        debugging_prompt = (
                            f"GitHub Issue: {state.issue_title}\n"
                            f"Description:\n{state.issue_body}\n\n"
                            f"The tests failed. Here is the last test run details:\n"
                            f"Command: {state.test_command}\n"
                            f"Output:\n{state.last_test_output}\n\n"
                            f"Please diagnose and fix the test failures. You must use your tools to modify the files (using `file_edit`) and re-run the tests (using `shell_exec`)."
                        )
                        messages.append(
                            types.Content(
                                role="user",
                                parts=[types.Part.from_text(text=debugging_prompt)],
                            )
                        )

                    initialized_mode = state.current_mode
                    initialized_attempt = state.attempt_number
                    tests_at_attempt_start = len(state.test_history)
                    force_tool_next_turn = False

                console.print(
                    Panel(
                        f"Mode: [bold cyan]{state.current_mode.value}[/bold cyan] | Attempt: [bold yellow]{state.attempt_number}/{state.max_attempts}[/bold yellow]",
                        title="[bold]autodev - Agent Loop Step[/bold]",
                        border_style="cyan",
                    )
                )

                # Inject current state context to keep agent aligned
                state_prefix = (
                    f"{state.to_formatted_summary()}\n"
                    f"Please proceed with the next step using your tools."
                )

                # We temporarily append this to the message list for the API call, but we don't save it to the permanent history
                # so we don't clutter the context window with repeated state summaries.
                current_messages = list(messages)
                if current_messages and current_messages[-1].role == "user":
                    last_msg = current_messages[-1]
                    new_parts = list(last_msg.parts)
                    new_parts.append(types.Part.from_text(text=f"\n\n{state_prefix}"))
                    current_messages[-1] = types.Content(role="user", parts=new_parts)
                else:
                    current_messages.append(
                        types.Content(
                            role="user", parts=[types.Part.from_text(text=state_prefix)]
                        )
                    )

                # Generate content
                system_prompt = self._get_system_prompt_for_mode(state.current_mode)
                allowed_tools = self._get_tools_for_mode(toolkit, state.current_mode)

                has_run_test_in_current_attempt = (
                    len(state.test_history) > tests_at_attempt_start
                )
                tool_config = None
                if force_tool_next_turn:
                    allowed_funcs = None
                    if state.current_mode == Mode.CODING:
                        allowed_funcs = ["file_write", "file_edit"]
                    elif state.current_mode in (Mode.TESTING, Mode.DEBUGGING):
                        allowed_funcs = ["shell_exec"]

                    tool_config = types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(
                            mode="ANY", allowed_function_names=allowed_funcs
                        )
                    )

                if tool_config:
                    logger.info(
                        f"Forcing tool usage in mode {state.current_mode} (mode='ANY', allowed_funcs={allowed_funcs})"
                    )

                llm_res = self.llm.generate(
                    contents=current_messages,
                    system_instruction=system_prompt,
                    tools=allowed_tools,
                    tool_config=tool_config,
                )

                if llm_res.text:
                    # Suppress duplicate printing of the plan in interactive mode (it will be shown in the Human Approval panel)
                    is_planning_transition = False
                    if (
                        state.current_mode == Mode.PLANNING
                        and self.settings.interactive
                    ):
                        text_lower = llm_res.text.lower()
                        if (
                            "### implementation plan" in text_lower
                            or "plan is ready" in text_lower
                            or "plan is complete" in text_lower
                        ):
                            is_planning_transition = True

                    if not is_planning_transition:
                        console.print("\n[bold purple]Agent Thinking:[/bold purple]")
                        console.print(Markdown(llm_res.text))
                        console.print()

                if llm_res.tool_calls:
                    consecutive_identical_responses = 0
                    force_tool_next_turn = False
                    # Model called tools: execute and append call/response to history
                    if llm_res.raw_content:
                        messages.append(llm_res.raw_content)
                    else:
                        model_parts = []
                        if llm_res.text:
                            model_parts.append(types.Part.from_text(text=llm_res.text))

                        for call in llm_res.tool_calls:
                            model_parts.append(
                                types.Part(
                                    function_call=types.FunctionCall(
                                        name=call["name"], args=call["args"]
                                    )
                                )
                            )
                        messages.append(types.Content(role="model", parts=model_parts))

                    # Execute calls
                    tool_response_parts = []
                    for call in llm_res.tool_calls:
                        tool_name = call["name"]
                        tool_args = call["args"]

                        console.print(
                            f"🔧 Calling Tool: [bold yellow]{tool_name}[/bold yellow] with {tool_args}"
                        )
                        res: ToolResult = registry.execute(tool_name, tool_args)

                        # Inspect and update state dynamically based on tool executions
                        self._process_tool_execution_state(
                            state, tool_name, tool_args, res
                        )

                        if res.success:
                            console.print(
                                f"✅ Success (Output length: {len(res.output)} chars)"
                            )
                            tool_response_parts.append(
                                types.Part(
                                    function_response=types.FunctionResponse(
                                        name=tool_name, response={"result": res.output}
                                    )
                                )
                            )
                        else:
                            console.print(f"❌ Error: [red]{res.error}[/red]")
                            tool_response_parts.append(
                                types.Part(
                                    function_response=types.FunctionResponse(
                                        name=tool_name,
                                        response={"result": f"Error: {res.error}"},
                                    )
                                )
                            )
                    messages.append(
                        types.Content(role="user", parts=tool_response_parts)
                    )

                    # Bug #1: Tool-call dedup / perseveration detection
                    call_sig = hashlib.md5(
                        json.dumps(
                            [(c["name"], c["args"]) for c in llm_res.tool_calls],
                            sort_keys=True,
                        ).encode()
                    ).hexdigest()
                    recent_tool_signatures.append(call_sig)

                    repeat_threshold = self.settings.tool_repeat_threshold
                    if len(recent_tool_signatures) >= repeat_threshold:
                        last_n = list(recent_tool_signatures)[-repeat_threshold:]
                        if len(set(last_n)) == 1:
                            tool_stall_warnings += 1
                            max_warnings = self.settings.max_tool_stall_warnings
                            nudge = (
                                f"You have called the exact same tool(s) with identical arguments {repeat_threshold} times in a row. "
                                f"The results will not change. Please try a different approach, read a different file, or formulate your plan. "
                                f"(Warning {tool_stall_warnings}/{max_warnings})"
                            )
                            console.print(
                                f"[bold red]Stall detected: {nudge}[/bold red]"
                            )
                            messages.append(
                                types.Content(
                                    role="user",
                                    parts=[types.Part.from_text(text=nudge)],
                                )
                            )
                            if tool_stall_warnings >= max_warnings:
                                console.print(
                                    "[bold red]Max stall warnings exceeded. Failing.[/bold red]"
                                )
                                state.current_mode = Mode.FAILED
                                state.add_error(
                                    "Agent stuck in tool-call loop — same tool called repeatedly with identical args."
                                )
                                break

                else:
                    # Model returned text only: transition modes
                    text = llm_res.text.lower()
                    is_ready = False
                    prompt_msg = "You returned a text response without calling any tools. Please use the appropriate tools to proceed with your task."

                    if state.current_mode == Mode.PLANNING:
                        is_ready = (
                            "### implementation plan" in text
                            or "plan is ready" in text
                            or "plan is complete" in text
                        )
                        prompt_msg = "You returned a text response without proposing a final implementation plan. Please explore the codebase further, formulate a plan, and write it under a '### IMPLEMENTATION PLAN' header."
                    elif state.current_mode == Mode.CODING:
                        is_ready = (
                            "ready for testing" in text
                            or "coding is complete" in text
                            or "coding is finished" in text
                            or "coding is done" in text
                        )
                        if is_ready and not state.files_modified:
                            is_ready = False
                            prompt_msg = "You have not modified any files in the workspace. You must use the file modification tools (like `file_write` or `file_edit`) to actually write your changes to disk before transitioning."
                        else:
                            prompt_msg = "You returned a text response without completing the coding phase. Please use the tools to modify the files, verify them with git_diff, and then announce when you are ready for testing."
                    elif state.current_mode == Mode.TESTING:
                        is_ready = (
                            "tests passed" in text
                            or "tests have passed" in text
                            or "successfully passed" in text
                            or "tests failed" in text
                            or "test failure" in text
                            or "testing is complete" in text
                        )
                        if is_ready and not state.test_history:
                            is_ready = False
                            prompt_msg = "You have not executed any tests in the workspace. You MUST run the tests using `shell_exec` (e.g. `pytest` or `python -m unittest`) to verify the changes before transitioning."
                        else:
                            prompt_msg = "You returned a text response without running or finalizing the tests. Please run the tests using `shell_exec` and report the results."
                    elif state.current_mode == Mode.DEBUGGING:
                        is_ready = (
                            "tests passed" in text
                            or "tests have passed" in text
                            or "successfully passed" in text
                            or "debugging is complete" in text
                            or "fix is complete" in text
                            or "unable to fix" in text
                            or "cannot fix" in text
                        )
                        if is_ready and not has_run_test_in_current_attempt:
                            is_ready = False
                            prompt_msg = "You have not run any tests in this debugging attempt. You must execute your test suite using `shell_exec` to verify the fix before transitioning."
                        else:
                            prompt_msg = "You returned a text response without finishing debugging. Please modify the files to fix the issue and run tests to verify."

                    # Check for infinite loops on identical responses
                    # Bug #3: Fuzzy similarity check instead of exact match
                    text_stripped = llm_res.text.strip()
                    if last_text_response:
                        similarity = difflib.SequenceMatcher(
                            None, text_stripped, last_text_response
                        ).ratio()
                        if similarity > 0.90:
                            consecutive_identical_responses += 1
                        else:
                            consecutive_identical_responses = 1
                    else:
                        consecutive_identical_responses = 1
                    last_text_response = text_stripped

                    if consecutive_identical_responses >= 3:
                        console.print(
                            "[bold red]Detecting infinite loop: Model repeated the same text response 3 times. Failsafe activated.[/bold red]"
                        )
                        state.current_mode = Mode.FAILED
                        state.add_error(
                            "Agent got stuck in an infinite loop repeating the same response."
                        )
                        break

                    # Append response to history
                    if llm_res.raw_content:
                        messages.append(llm_res.raw_content)
                    else:
                        messages.append(
                            types.Content(
                                role="model",
                                parts=[types.Part.from_text(text=llm_res.text)],
                            )
                        )

                    if is_ready:
                        console.print(
                            "[bold yellow]Agent declared transition readiness. Transitioning mode...[/bold yellow]"
                        )
                        self._handle_mode_transition(state, llm_res.text, messages)
                        force_tool_next_turn = False
                    else:
                        console.print(
                            "[bold yellow]Agent returned text but did not request transition or checks failed. Prompting to continue...[/bold yellow]"
                        )
                        messages.append(
                            types.Content(
                                role="user",
                                parts=[types.Part.from_text(text=prompt_msg)],
                            )
                        )
                        force_tool_next_turn = True

            # Execution complete!
            if state.current_mode == Mode.DONE:
                if self.settings.is_github_configured:
                    console.print(
                        "[bold green]Creating Pull Request on GitHub...[/bold green]"
                    )
                    try:
                        pr_creator = PRCreator(self.settings.github_token)

                        # Generate a professional PR body using Gemini
                        if state.plan:
                            try:
                                console.print(
                                    "[bold green]Generating professional Pull Request description...[/bold green]"
                                )
                                pr_prompt = (
                                    f"Draft a clean, professional Pull Request description (Markdown) for the following changes:\n"
                                    f"Issue: {state.issue_title}\n"
                                    f"Issue Description:\n{state.issue_body}\n\n"
                                    f"Files Modified: {list(state.files_modified.keys()) if state.files_modified else 'None'}\n\n"
                                    f"Implementation Plan used:\n{state.plan}\n\n"
                                    f"Please structure it with: '### Overview', '### Key Changes' (list modified files and a brief summary of changes), and '### Verification' (mention that all unit tests have passed).\n\n"
                                    f"IMPORTANT: Output ONLY the markdown description itself. Do not include any introductory or concluding conversational text, explanations, or preambles (e.g. do not say 'Here is a clean and professional Pull Request description...')."
                                )
                                pr_body_res = self.llm.generate(contents=pr_prompt)
                                pr_body = pr_body_res.text.strip()
                            except Exception as e:
                                logger.warning(
                                    f"Failed to generate professional PR body: {e}. Falling back to default plan."
                                )
                                pr_body = state.plan or "Automated fix by autodev."
                        else:
                            pr_body = "Automated fix by autodev."

                        pr_url = pr_creator.create_pull_request(
                            sandbox=self.sandbox,
                            workspace=workspace,
                            owner=owner,
                            repo_name=repo,
                            issue_number=number,
                            branch_name=branch_name,
                            title=state.issue_title,
                            body=pr_body,
                        )
                        state.pr_url = pr_url
                        console.print(
                            f"[bold green]PR Created Successfully:[/bold green] {pr_url}"
                        )
                    except Exception as e:
                        logger.error(f"Failed to create PR: {e}")
                        state.add_error(f"PR creation failed: {e}")
                else:
                    console.print(
                        "[yellow]GitHub Token not configured. Skipping PR creation.[/yellow]"
                    )

            console.print(
                Panel(
                    f"Final Status: [bold]{state.current_mode.value}[/bold]\n"
                    f"Attempts Used: {state.attempt_number}/{state.max_attempts}\n"
                    f"Files Modified: {list(state.files_modified.keys())}\n"
                    f"PR URL: {state.pr_url or 'None'}",
                    title="[bold green]autodev - Run Completed[/bold green]",
                    border_style="green",
                )
            )

        finally:
            console.print("[bold yellow]Tearing down workspace...[/bold yellow]")
            self.workspace_mgr.teardown_workspace(workspace)

        return state

    def _process_tool_execution_state(
        self,
        state: AgentState,
        tool_name: str,
        args: Dict[str, Any],
        result: ToolResult,
    ):
        """Monitors tool executions and updates structured history (modified files, test outputs)."""
        if not result.success:
            state.add_error(f"Tool {tool_name} failed: {result.error}")
            return

        if tool_name in ("file_edit", "file_write"):
            path = args.get("path")
            if path:
                if path not in state.files_modified:
                    state.files_modified[path] = []
                state.files_modified[path].append(
                    EditRecord(
                        search=args.get("search", ""),
                        replace=args.get("replace", args.get("content", "")),
                        timestamp=time.time(),
                    )
                )

        elif tool_name == "shell_exec":
            cmd = args.get("command", "")
            # Only record as test result when in TESTING or DEBUGGING mode
            if state.current_mode in (Mode.TESTING, Mode.DEBUGGING):
                exit_code = 0
                if "timed out" in result.output:
                    exit_code = 124
                elif "exit code:" in result.output:
                    matches = re.findall(r"exit code:\s*(-?\d+)", result.output)
                    if matches:
                        exit_code = int(matches[-1])
                    else:
                        exit_code = 1 if "FAILED" in result.output else 0
                else:
                    exit_code = (
                        1
                        if (
                            "FAILED" in result.output
                            or "ERRORS" in result.output
                            or "Traceback" in result.output
                        )
                        else 0
                    )

                passed = exit_code == 0
                state.test_history.append(
                    TestResult(
                        command=cmd,
                        passed=passed,
                        stdout=result.output,
                        stderr="",
                        exit_code=exit_code,
                    )
                )
                state.test_command = cmd

    def _handle_mode_transition(
        self, state: AgentState, text_response: str, messages: List[types.Content]
    ):
        """Controls state transitions when the model returns a final text response (no tool calls)."""
        if state.current_mode == Mode.PLANNING:
            state.plan = text_response

            # Interactive mode (HITL) gate
            if self.settings.interactive:
                console.print(
                    Panel(
                        Markdown(text_response),
                        title="[bold cyan]Human Approval Required[/bold cyan]",
                        border_style="cyan",
                    )
                )
                # Prompt user on CLI
                approve = (
                    input("Do you approve this plan? (y/n/feedback): ").strip().lower()
                )
                if approve in ("y", "yes"):
                    state.plan_approved = True
                    state.current_mode = Mode.CODING
                else:
                    feedback = input("Provide feedback for the agent: ").strip()
                    state.plan = None  # reset plan
                    state.add_error(f"User rejected plan. Feedback: {feedback}")
                    # Append feedback as a user turn in history so LLM is aware of rejection details
                    messages.append(
                        types.Content(
                            role="user",
                            parts=[
                                types.Part.from_text(
                                    text=f"Your proposed plan was rejected. User feedback: {feedback}"
                                )
                            ],
                        )
                    )
            else:
                state.plan_approved = True
                state.current_mode = Mode.CODING

        elif state.current_mode == Mode.CODING:
            # Model says it's done coding, proceed to testing
            state.current_mode = Mode.TESTING

        elif state.current_mode == Mode.TESTING:
            # Model finished testing. Check if tests passed
            if state.last_test_passed:
                state.current_mode = Mode.DONE
            else:
                state.current_mode = Mode.DEBUGGING

        elif state.current_mode == Mode.DEBUGGING:
            # Check if tests pass now
            if state.last_test_passed:
                state.current_mode = Mode.DONE
            else:
                # If tests failed, check retry budget
                if state.attempt_number < state.max_attempts:
                    state.attempt_number += 1
                    # Transition back to CODING or stay in DEBUGGING to apply another fix
                    state.current_mode = Mode.DEBUGGING
                else:
                    state.current_mode = Mode.FAILED
