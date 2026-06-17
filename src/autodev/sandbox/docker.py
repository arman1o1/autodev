import logging
import time
import subprocess
import os
import re
import stat
import shutil
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
import docker
from docker.errors import ImageNotFound, APIError

logger = logging.getLogger("autodev.sandbox")


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool


def _handle_remove_readonly(func, path, exc):
    """Error handler for shutil.rmtree to handle read-only files (e.g. .git on Windows)."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


class SandboxManager:
    def __init__(
        self,
        image_name: str = "autodev-sandbox",
        memory_limit: str = "512m",
        timeout: int = 300,
        allow_local_shell: bool = False,
    ):
        self.image_name = image_name
        self.memory_limit = memory_limit
        self.timeout = timeout
        self.allow_local_shell = allow_local_shell
        self.use_docker = True
        try:
            self.client = docker.from_env()
        except Exception as e:
            logger.warning(
                f"Docker daemon is not running or inaccessible. Falling back to local subprocess sandbox! Error: {e}"
            )
            self.use_docker = False
            self.client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Cleanup is handled per-container via stop_and_remove_container
        return False

    def get_repo_path(self, workspace_id: str) -> str:
        """Returns the appropriate repository path depending on Docker usage."""
        if self.use_docker:
            return "/workspace/repo"
        else:
            # Local workspace path on the host
            local_path = Path("workspaces") / workspace_id / "repo"
            return str(local_path.resolve())

    def build_image_if_needed(self, dockerfile_dir: str = ".") -> str:
        """Builds the sandbox Docker image if it doesn't already exist. Only applicable if use_docker=True."""
        if not self.use_docker:
            return ""
        tag = f"{self.image_name}:latest"
        try:
            self.client.images.get(tag)
            logger.info(f"Docker image {tag} already exists.")
            return tag
        except ImageNotFound:
            logger.info(f"Docker image {tag} not found. Building...")
            try:
                # Build image from local Dockerfile
                image, build_logs = self.client.images.build(
                    path=dockerfile_dir, tag=tag, rm=True
                )
                for log in build_logs:
                    if "stream" in log:
                        logger.debug(log["stream"].strip())
                logger.info(f"Docker image {tag} built successfully.")
                return tag
            except Exception as e:
                logger.error(f"Failed to build Docker image: {e}")
                raise RuntimeError(f"Could not build Docker image {tag}") from e

    def start_container(self, workspace_name: str) -> str:
        """Starts a detached sandbox container (Docker) or initializes a local directory (Local fallback)."""
        if not self.use_docker:
            # Local fallback: setup directories
            workspace_dir = Path("workspaces") / workspace_name
            if workspace_dir.exists():
                logger.info(
                    f"Workspace directory {workspace_dir} already exists. Cleaning it up for a clean run."
                )
                try:
                    shutil.rmtree(workspace_dir, onerror=_handle_remove_readonly)
                except Exception as e:
                    logger.warning(
                        f"Failed to clear existing directory {workspace_dir}: {e}"
                    )

            workspace_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Initialized local workspace directory: {workspace_dir}")
            return f"local-{workspace_name}"

        self.build_image_if_needed()
        tag = f"{self.image_name}:latest"
        container_name = f"autodev-sandbox-{workspace_name}-{int(time.time())}"

        try:
            container = self.client.containers.run(
                image=tag,
                command="tail -f /dev/null",  # keeps container alive
                name=container_name,
                detach=True,
                mem_limit=self.memory_limit,
                nano_cpus=1000000000,  # Limit container to 1 CPU core
                network_mode="bridge",  # allows cloning via git
                restart_policy={"Name": "no"},
            )
            logger.info(
                f"Started sandbox container {container.name} (ID: {container.short_id})"
            )
            return container.id
        except APIError as e:
            logger.error(f"Failed to start container: {e}")
            raise RuntimeError("Failed to spawn sandbox container.") from e

    def exec_command(
        self,
        container_id: str,
        command: str,
        workdir: Optional[str] = None,
        timeout: int = 120,
    ) -> ExecResult:
        """Executes a command inside the container or locally as a fallback, returning output and status."""
        if workdir is None:
            if not self.use_docker or container_id.startswith("local-"):
                workspace_name = container_id.replace("local-", "", 1)
                workdir = str((Path("workspaces") / workspace_name).resolve())
            else:
                workdir = "/workspace"

        if not self.use_docker or container_id.startswith("local-"):
            return self._exec_local(command, workdir, timeout)

        try:
            container = self.client.containers.get(container_id)
        except Exception as e:
            logger.error(f"Container {container_id} not found: {e}")
            raise RuntimeError(f"Container {container_id} is not running.") from e

        # Wrap the command using timeout utility inside the container to prevent hangs
        escaped_command = command.replace("'", "'\\''")
        full_command = f"timeout {timeout} /bin/sh -c '{escaped_command}'"

        try:
            # Execute command inside container
            exec_id = self.client.api.exec_create(
                container=container.id,
                cmd=["/bin/sh", "-c", f"cd {workdir} && {full_command}"],
                user="sandbox_user",
            )

            # Start exec and get output stream
            output_stream = self.client.api.exec_start(
                exec_id, stream=False, demux=True
            )

            # Wait for execution and inspect exit status
            exec_info = self.client.api.exec_inspect(exec_id)
            exit_code = exec_info.get("ExitCode", -1)
        except Exception as e:
            logger.error(f"Error during command execution in container: {e}")
            return ExecResult(stdout="", stderr=str(e), exit_code=-1, timed_out=False)

        stdout = ""
        stderr = ""
        if output_stream:
            raw_stdout, raw_stderr = output_stream
            if raw_stdout:
                stdout = raw_stdout.decode("utf-8", errors="replace")
            if raw_stderr:
                stderr = raw_stderr.decode("utf-8", errors="replace")

        return ExecResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            timed_out=(exit_code == 124),
        )

    def _exec_local(self, command: str, workdir: str, timeout: int) -> ExecResult:
        """Executes a command locally on the host using subprocess (fallback mode)."""
        if not self.allow_local_shell:
            raise PermissionError(
                "Local command execution on the host is disabled by default for security. "
                "To enable it, set allow_local_shell to True or run inside Docker."
            )

        scrubbed_cmd = re.sub(
            r"x-access-token:[a-zA-Z0-9_]+@", "x-access-token:***@", command
        )
        logger.debug(f"Local subprocess execution (workdir={workdir}): {scrubbed_cmd}")

        # Ensure workdir exists locally
        os.makedirs(workdir, exist_ok=True)

        try:
            # Run the command with timeout limit
            res = subprocess.run(
                command,
                cwd=workdir,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            return ExecResult(
                stdout=res.stdout,
                stderr=res.stderr,
                exit_code=res.returncode,
                timed_out=False,
            )
        except subprocess.TimeoutExpired as e:
            logger.warning(f"Local command execution timed out: {command}")
            # Decode output if available
            stdout_str = (
                e.stdout
                if isinstance(e.stdout, str)
                else (e.stdout.decode("utf-8", errors="replace") if e.stdout else "")
            )
            stderr_str = (
                e.stderr
                if isinstance(e.stderr, str)
                else (e.stderr.decode("utf-8", errors="replace") if e.stderr else "")
            )
            return ExecResult(
                stdout=stdout_str, stderr=stderr_str, exit_code=124, timed_out=True
            )
        except Exception as e:
            logger.error(f"Error executing command locally: {e}")
            return ExecResult(stdout="", stderr=str(e), exit_code=-1, timed_out=False)

    def stop_and_remove_container(self, container_id: str):
        """Stops and removes the sandbox container (or cleans up local directory for local mode)."""
        if not self.use_docker or container_id.startswith("local-"):
            # Local clean up
            workspace_name = container_id.replace("local-", "")
            workspace_dir = Path("workspaces") / workspace_name
            if workspace_dir.exists():
                logger.info(f"Cleaning up local workspace directory: {workspace_dir}")
                try:
                    # shutil.rmtree might fail due to read-only files (e.g. .git directory files)
                    # We can use a helper or force delete
                    shutil.rmtree(workspace_dir, onerror=_handle_remove_readonly)
                except Exception as e:
                    logger.warning(
                        f"Failed to delete local workspace directory {workspace_dir}: {e}"
                    )
            return

        try:
            container = self.client.containers.get(container_id)
            logger.info(f"Stopping and removing container {container.name}")
            container.stop(timeout=5)
            container.remove(force=True)
        except Exception as e:
            logger.warning(f"Error removing container {container_id}: {e}")
