import os
import json
import time
import logging
from pathlib import Path
from typing import Dict, Any
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from autodev.config import AutodevSettings
from autodev.llm.client import GeminiClient
from autodev.sandbox.docker import SandboxManager
from autodev.agent.core import AutodevAgent
from autodev.agent.modes import Mode

logger = logging.getLogger("autodev.eval")
console = Console()


class BenchmarkRunner:
    def __init__(self, settings: AutodevSettings):
        self.settings = settings
        # Ensure interactive is turned off during benchmark runs!
        self.settings.interactive = False

    def run_benchmark(
        self, benchmark_file: str, output_dir: str = "benchmarks/results"
    ) -> Dict[str, Any]:
        """Loads a list of issues from benchmark_file, runs the agent on each, and records results."""
        if not os.path.exists(benchmark_file):
            raise FileNotFoundError(f"Benchmark file '{benchmark_file}' not found.")

        with open(benchmark_file, "r", encoding="utf-8") as f:
            issues = json.load(f)

        if not isinstance(issues, list):
            raise ValueError("Benchmark file must contain a list of issue objects.")

        console.print(
            Panel(
                f"Loaded [bold cyan]{len(issues)}[/bold cyan] issues from [bold green]{benchmark_file}[/bold green].\n"
                f"Executing evaluation using model: [green]{self.settings.gemini_model}[/green]",
                title="[bold green]autodev - Benchmark Evaluation[/bold green]",
                border_style="green",
            )
        )

        results = []
        total_issues = len(issues)
        successes = 0
        failures = 0
        total_time = 0.0

        # Initialize clients
        llm_client = GeminiClient(
            api_key=self.settings.gemini_api_key, model_name=self.settings.gemini_model
        )
        sandbox_mgr = SandboxManager(
            memory_limit=self.settings.sandbox_memory_limit,
            timeout=self.settings.sandbox_timeout,
        )

        for i, issue_data in enumerate(issues, 1):
            issue_url = issue_data.get("issue_url")
            issue_title = issue_data.get("issue_title", f"Issue {i}")
            issue_body = issue_data.get("issue_body", "")
            repo_url = issue_data.get("repo_url")

            console.print(
                f"\n[bold yellow]--- Running Evaluation {i}/{total_issues}: {issue_title} ---[/bold yellow]"
            )

            agent = AutodevAgent(
                settings=self.settings, llm_client=llm_client, sandbox_mgr=sandbox_mgr
            )

            start_time = time.time()
            success = False
            error_msg = None
            final_mode = "FAILED"
            attempts_used = 1
            modified_files = []

            try:
                state = agent.solve(
                    issue_url=issue_url,
                    issue_title=issue_title,
                    issue_body=issue_body,
                    repo_url=repo_url,
                )
                success = state.current_mode == Mode.DONE
                final_mode = state.current_mode.value
                attempts_used = state.attempt_number
                modified_files = list(state.files_modified.keys())
                if state.errors_encountered and not success:
                    error_msg = "; ".join(state.errors_encountered)
            except Exception as e:
                logger.error(
                    f"Execution error on issue {issue_title}: {e}", exc_info=True
                )
                error_msg = str(e)

            duration = time.time() - start_time
            total_time += duration

            if success:
                successes += 1
                status_str = "[bold green]SUCCESS[/bold green]"
            else:
                failures += 1
                status_str = "[bold red]FAILED[/bold red]"

            console.print(
                f"Status: {status_str} | Duration: {duration:.2f}s | Attempts: {attempts_used}"
            )

            results.append(
                {
                    "issue_url": issue_url,
                    "issue_title": issue_title,
                    "success": success,
                    "final_mode": final_mode,
                    "attempts_used": attempts_used,
                    "duration_seconds": duration,
                    "modified_files": modified_files,
                    "error": error_msg,
                }
            )

        # Calculate statistics
        success_rate = (successes / total_issues) * 100 if total_issues > 0 else 0
        avg_duration = total_time / total_issues if total_issues > 0 else 0

        summary = {
            "benchmark_file": benchmark_file,
            "total_issues": total_issues,
            "successes": successes,
            "failures": failures,
            "success_rate_percent": success_rate,
            "total_time_seconds": total_time,
            "average_duration_seconds": avg_duration,
            "results": results,
        }

        # Save summary report
        os.makedirs(output_dir, exist_ok=True)
        filename = f"report-{Path(benchmark_file).stem}-{int(time.time())}.json"
        report_path = Path(output_dir) / filename
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

        # Print summary table
        self._print_summary_table(summary, str(report_path))

        return summary

    def _print_summary_table(self, summary: Dict[str, Any], report_path: str):
        """Prints a beautiful Rich table summarizing the benchmark results."""
        table = Table(
            title="[bold green]autodev Evaluation Summary[/bold green]",
            show_header=True,
            header_style="bold magenta",
        )
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Benchmark Source", summary["benchmark_file"])
        table.add_row("Total Issues Evaluated", str(summary["total_issues"]))
        table.add_row("Successes", f"[bold green]{summary['successes']}[/bold green]")
        table.add_row("Failures", f"[bold red]{summary['failures']}[/bold red]")
        table.add_row(
            "Success Rate", f"[bold]{summary['success_rate_percent']:.1f}%[/bold]"
        )
        table.add_row("Total Time", f"{summary['total_time_seconds']:.2f}s")
        table.add_row(
            "Average Time Per Issue", f"{summary['average_duration_seconds']:.2f}s"
        )
        table.add_row("Report Saved To", report_path)

        console.print("\n")
        console.print(table)
        console.print("\n")
