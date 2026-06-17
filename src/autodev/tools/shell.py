import logging
import re

logger = logging.getLogger("autodev.tools.shell")

# Safety blocklist for dangerous shell patterns
BLOCKED_PATTERNS = [
    r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+|)(/|\*)",  # rm -rf / or rm -f / or rm /* variants
    r"chmod\s+.*777",  # Avoid unsafe permission changes
    r":\(\){\s*:\|:\s*&\s*}\s*;\s*:",  # Fork bomb variants
    r"\bshutdown\b",  # Power down command
    r"\breboot\b",  # Reboot
    r"\bcurl\b.*\|.*\bsh\b",  # curl pipe to shell
    r"\bwget\b.*\|.*\bsh\b",  # wget pipe to shell
    r"\bmkfs\b",  # Format filesystem
    r"\bdd\b.*of=/dev/",  # Direct disk write
]


def shell_exec(sandbox, workspace, command: str, timeout: int = 120) -> str:
    """Executes a shell command inside the sandboxed workspace.

    Warning: Command runs in a restricted bash environment. Do not use interactive commands.
    """
    # Check blocklist
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, command):
            raise ValueError(
                f"Command execution blocked: command contains unsafe pattern matching '{pattern}'"
            )

    res = sandbox.exec_command(
        container_id=workspace.container_id,
        command=command,
        workdir=workspace.repo_path,
        timeout=timeout,
    )

    output = ""
    if res.stdout:
        output += f"--- STDOUT ---\n{res.stdout}"
    if res.stderr:
        output += f"--- STDERR ---\n{res.stderr}"

    if res.timed_out:
        output += f"\n[Command execution timed out after {timeout} seconds]"

    # Append exit code for state tracking and LLM observation
    output += f"\nexit code: {res.exit_code}"

    # Truncate output if it's excessively large to avoid blowing up context window
    max_chars = 100000  # ~25k tokens max
    if len(output) > max_chars:
        half = max_chars // 2
        output = (
            output[:half]
            + f"\n\n... [TRUNCATED {len(output) - max_chars} CHARACTERS] ...\n\n"
            + output[-half:]
        )

    return output
