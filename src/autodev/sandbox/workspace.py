import logging
import os
import shlex
from dataclasses import dataclass
from typing import Optional
from autodev.sandbox.docker import SandboxManager

logger = logging.getLogger("autodev.workspace")


@dataclass
class Workspace:
    id: str  # Unique workspace ID/name
    repo_url: str  # Target GitHub repo URL
    container_id: str  # Docker container ID
    repo_path: str = "/workspace/repo"
    branch_name: Optional[str] = None


class WorkspaceManager:
    def __init__(self, sandbox_manager: SandboxManager):
        self.sandbox = sandbox_manager

    def setup_workspace(
        self, workspace_id: str, repo_url: str, branch_name: Optional[str] = None
    ) -> Workspace:
        """Clones a repository, checks out a branch, and installs dependencies inside the sandbox."""
        logger.info(f"Setting up workspace '{workspace_id}' for repo: {repo_url}")

        # 1. Start sandbox container
        container_id = self.sandbox.start_container(workspace_id)
        repo_path = self.sandbox.get_repo_path(workspace_id)

        workspace = Workspace(
            id=workspace_id,
            repo_url=repo_url,
            container_id=container_id,
            repo_path=repo_path,
            branch_name=branch_name,
        )

        try:
            # 2. Clone repository
            logger.info("Cloning repository inside sandbox...")
            if self.sandbox.use_docker:
                clone_cmd = f"git clone {shlex.quote(repo_url)} {shlex.quote(workspace.repo_path)}"
            else:
                clone_cmd = f'git clone "{repo_url}" "{workspace.repo_path}"'
            res = self.sandbox.exec_command(container_id, clone_cmd)
            if res.exit_code != 0:
                raise RuntimeError(f"Failed to clone repository: {res.stderr}")

            # 2b. Configure Git exclusions to prevent committing python cache or temporary test files
            self._configure_git_exclude(workspace)

            # 3. Create or checkout branch
            if branch_name:
                logger.info(f"Creating/checking out branch '{branch_name}'...")
                # Try checkout first, if fails, create branch
                if self.sandbox.use_docker:
                    checkout_cmd = f"git checkout -b {shlex.quote(branch_name)} || git checkout {shlex.quote(branch_name)}"
                else:
                    checkout_cmd = f'git checkout -b "{branch_name}" || git checkout "{branch_name}"'
                res = self.sandbox.exec_command(
                    container_id, checkout_cmd, workdir=workspace.repo_path
                )
                if res.exit_code != 0:
                    raise RuntimeError(f"Failed to setup branch: {res.stderr}")

            # 4. Install dependencies
            logger.info("Auto-detecting and installing project dependencies...")
            self._install_dependencies(workspace)

            return workspace

        except Exception as e:
            logger.error(f"Error during workspace setup, tearing down container: {e}")
            self.teardown_workspace(workspace)
            raise

    def _configure_git_exclude(self, workspace: Workspace):
        """Appends common build and cache directories to .git/info/exclude."""
        exclude_path = os.path.join(workspace.repo_path, ".git", "info", "exclude")
        patterns = [
            "",
            "# autodev exclusions to prevent pushing cache/build artifacts",
            "__pycache__/",
            "*.pyc",
            "*.pyo",
            "*.pyd",
            ".pytest_cache/",
            "*.egg-info/",
            "dist/",
            "build/",
            "node_modules/",
            ".venv/",
            "venv/",
        ]
        content = "\n".join(patterns) + "\n"

        if not self.sandbox.use_docker:
            try:
                os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
                with open(exclude_path, "a", encoding="utf-8") as f:
                    f.write(content)
                logger.info("Configured Git exclusions locally.")
            except Exception as e:
                logger.warning(f"Failed to configure Git exclusions locally: {e}")
        else:
            import base64

            b64_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")
            b64_path = base64.b64encode(exclude_path.encode("utf-8")).decode("utf-8")
            py_code = (
                "import os, base64\n"
                f"p = base64.b64decode('{b64_path}').decode('utf-8')\n"
                "os.makedirs(os.path.dirname(p), exist_ok=True)\n"
                f"content = base64.b64decode('{b64_content}').decode('utf-8')\n"
                "with open(p, 'a', encoding='utf-8') as f:\n"
                "    f.write(content)\n"
            )
            b64_py_code = base64.b64encode(py_code.encode("utf-8")).decode("utf-8")
            run_cmd = f"echo '{b64_py_code}' | base64 -d | python3"
            res = self.sandbox.exec_command(workspace.container_id, run_cmd)
            if res.exit_code != 0:
                logger.warning(
                    f"Failed to configure Git exclusions inside Docker: {res.stderr or res.stdout}"
                )
            else:
                logger.info("Configured Git exclusions inside Docker container.")

    def _install_dependencies(self, workspace: Workspace):
        """Detects requirements.txt or pyproject.toml and installs dependencies via uv."""

        container_id = workspace.container_id
        path = workspace.repo_path

        if not self.sandbox.use_docker:
            has_pyproject = os.path.exists(os.path.join(path, "pyproject.toml"))
            has_requirements = os.path.exists(os.path.join(path, "requirements.txt"))
        else:
            # Check for pyproject.toml
            check_pyproject = self.sandbox.exec_command(
                container_id, "test -f pyproject.toml", workdir=path
            )
            # Check for requirements.txt
            check_reqs = self.sandbox.exec_command(
                container_id, "test -f requirements.txt", workdir=path
            )
            has_pyproject = check_pyproject.exit_code == 0
            has_requirements = check_reqs.exit_code == 0

        if has_pyproject:
            logger.info("Detected pyproject.toml. Installing with uv pip...")
            # Install in editable mode if it has a build backend, otherwise just install dependencies
            install_cmd = "uv pip install --system -e . || uv pip install --system ."
            res = self.sandbox.exec_command(container_id, install_cmd, workdir=path)
            if res.exit_code != 0:
                logger.warning(f"Failed to install via pyproject.toml: {res.stderr}")

        if has_requirements:
            logger.info("Detected requirements.txt. Installing dependencies via uv...")
            install_cmd = "uv pip install --system -r requirements.txt"
            res = self.sandbox.exec_command(container_id, install_cmd, workdir=path)
            if res.exit_code != 0:
                logger.warning(f"Failed to install requirements.txt: {res.stderr}")

        # If neither or if they failed, log a warning (user will need to run commands manually or let the agent handle it)
        if not has_pyproject and not has_requirements:
            logger.info(
                "No pyproject.toml or requirements.txt found. Skipping automatic dependency installation."
            )

    def teardown_workspace(self, workspace: Workspace):
        """Stops and removes the container associated with the workspace."""
        logger.info(f"Tearing down workspace '{workspace.id}'")
        self.sandbox.stop_and_remove_container(workspace.container_id)
