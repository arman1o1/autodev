import logging

logger = logging.getLogger("autodev.tools.git_ops")


def git_diff(sandbox, workspace) -> str:
    """Returns the current unstaged changes in the repository."""
    cmd = "git --no-pager diff"
    res = sandbox.exec_command(workspace.container_id, cmd, workdir=workspace.repo_path)
    if res.exit_code != 0:
        raise RuntimeError(f"Failed to run git diff: {res.stderr or res.stdout}")
    return res.stdout.strip() or "No uncommitted changes."


def git_status(sandbox, workspace) -> str:
    """Returns the current status of the working directory (modified, untracked files)."""
    cmd = "git --no-pager status"
    res = sandbox.exec_command(workspace.container_id, cmd, workdir=workspace.repo_path)
    if res.exit_code != 0:
        raise RuntimeError(f"Failed to run git status: {res.stderr or res.stdout}")
    return res.stdout.strip()


def git_log(sandbox, workspace, n: int = 5) -> str:
    """Returns the last n commit messages in the current branch."""
    if not isinstance(n, int) or n < 1:
        n = 5
    cmd = f"git --no-pager log -n {n} --oneline"
    res = sandbox.exec_command(workspace.container_id, cmd, workdir=workspace.repo_path)
    if res.exit_code != 0:
        raise RuntimeError(f"Failed to run git log: {res.stderr or res.stdout}")
    return res.stdout.strip()
