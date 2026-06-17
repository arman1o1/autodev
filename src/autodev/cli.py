import sys

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import typer
import json
from typing import Annotated, Optional
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from pydantic import ValidationError

from autodev.config import AutodevSettings
from autodev.llm.client import GeminiClient
from autodev.sandbox.docker import SandboxManager
from autodev.sandbox.workspace import Workspace
from autodev.tools.codebase import index_codebase
from autodev.agent.core import AutodevAgent
from autodev.evaluation.benchmark import BenchmarkRunner

app = typer.Typer(
    name="autodev",
    help="🤖 autodev: An autonomous AI software agent that solves GitHub issues.",
    no_args_is_help=True,
)
console = Console()


def load_settings_or_exit() -> AutodevSettings:
    """Helper to load settings or print validation errors and exit."""
    try:
        return AutodevSettings()
    except ValidationError as e:
        console.print(
            Panel(
                Text(f"Configuration Error:\n{str(e)}", style="bold red"),
                title="[bold red]Error Loading Config[/bold red]",
                border_style="red",
            )
        )
        console.print(
            "[yellow]Please check your environment variables or make sure you have a valid .env file.[/yellow]"
        )
        sys.exit(1)


@app.command()
def solve(
    issue_url: Annotated[str, typer.Argument(help="The GitHub issue URL to resolve.")],
    interactive: Annotated[
        bool, typer.Option("--interactive", "-i", help="Enable human checkpoints.")
    ] = False,
    model: Annotated[
        Optional[str],
        typer.Option("--model", "-m", help="Override the Gemini model to use."),
    ] = None,
):
    """Solve a GitHub issue end-to-end autonomously."""
    settings = load_settings_or_exit()
    if not settings.gemini_api_key:
        console.print(
            "[bold red]Error: GEMINI_API_KEY is required but not configured.[/bold red]"
        )
        sys.exit(1)
    if model:
        settings.gemini_model = model
    if interactive:
        settings.interactive = True

    console.print(
        Panel(
            f"Starting agent to solve: [bold blue]{issue_url}[/bold blue]\n"
            f"Model: [green]{settings.gemini_model}[/green]\n"
            f"Mode: [yellow]{'Interactive (HITL)' if settings.interactive else 'Fully Autonomous'}[/yellow]",
            title="[bold green]autodev - Run Initialization[/bold green]",
            border_style="green",
        )
    )

    try:
        llm_client = GeminiClient(
            api_key=settings.gemini_api_key, model_name=settings.gemini_model
        )
        sandbox_mgr = SandboxManager(
            memory_limit=settings.sandbox_memory_limit,
            timeout=settings.sandbox_timeout,
            allow_local_shell=settings.allow_local_shell,
        )
        agent = AutodevAgent(
            settings=settings, llm_client=llm_client, sandbox_mgr=sandbox_mgr
        )

        # Run agent
        state = agent.solve(issue_url=issue_url)

        if state.pr_url:
            console.print(f"[bold green]PR URL: {state.pr_url}[/bold green]")
        else:
            console.print(
                "[yellow]Agent finished without creating a Pull Request.[/yellow]"
            )
    except Exception as e:
        console.print(
            Panel(
                Text(f"Agent Execution Failed:\n{str(e)}", style="bold red"),
                title="[bold red]Execution Error[/bold red]",
                border_style="red",
            )
        )
        sys.exit(1)


@app.command()
def index(
    repo_path: Annotated[str, typer.Argument(help="Path to the repository to index.")],
):
    """Index a repository's codebase and extract symbol table."""
    load_settings_or_exit()
    console.print(f"[bold green]Indexing repository at:[/bold green] {repo_path}")

    try:
        sandbox = SandboxManager()
        sandbox.use_docker = False
        workspace = Workspace(
            id="local-index", repo_url="", container_id="local", repo_path=repo_path
        )
        index_json = index_codebase(sandbox, workspace)
        parsed = json.loads(index_json)
        console.print_json(data=parsed)
    except Exception as e:
        console.print(f"[bold red]Indexing failed:[/bold red] {e}")
        sys.exit(1)


@app.command()
def eval(
    benchmark_file: Annotated[
        str, typer.Argument(help="Path to the benchmark configuration file.")
    ],
    output_dir: Annotated[
        str,
        typer.Option("--output-dir", "-o", help="Directory to save the JSON reports."),
    ] = "benchmarks/results",
):
    """Run evaluation benchmark on SWE-bench Lite or custom issues."""
    settings = load_settings_or_exit()
    if not settings.gemini_api_key:
        console.print(
            "[bold red]Error: GEMINI_API_KEY is required but not configured.[/bold red]"
        )
        sys.exit(1)
    try:
        runner = BenchmarkRunner(settings)
        runner.run_benchmark(benchmark_file, output_dir)
    except Exception as e:
        console.print(f"[bold red]Evaluation failed:[/bold red] {e}")
        sys.exit(1)


if __name__ == "__main__":
    app()
