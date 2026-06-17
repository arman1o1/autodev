import logging
import base64
import time
from typing import Optional
from github import Github, Auth
from github.GithubException import GithubException
from autodev.sandbox.docker import SandboxManager
from autodev.sandbox.workspace import Workspace
import subprocess

logger = logging.getLogger("autodev.github.pr")


def get_auth_remote_url(repo_url: str, token: str) -> str:
    """Injects a GitHub token into an HTTPS clone URL for authenticated pushing."""
    if repo_url.startswith("https://github.com/"):
        return repo_url.replace(
            "https://github.com/", f"https://x-access-token:{token}@github.com/"
        )
    if repo_url.startswith("git@"):
        raise ValueError(
            f"SSH URLs are not supported for token-based auth. Use HTTPS URL instead: {repo_url}"
        )
    return repo_url


class PRCreator:
    def __init__(self, token: Optional[str] = None):
        self.token = token
        self.github = Github(auth=Auth.Token(token)) if token else None

    def create_pull_request(
        self,
        sandbox: SandboxManager,
        workspace: Workspace,
        owner: str,
        repo_name: str,
        issue_number: int,
        branch_name: str,
        title: str,
        body: str,
    ) -> str:
        """Pushes the local changes to remote and opens a pull request."""
        if not self.token or not self.github:
            raise RuntimeError("GitHub Token is required to open pull requests.")

        container_id = workspace.container_id
        repo_path = workspace.repo_path

        # 1. Check write access to the target repository
        logger.info("Verifying repository write access...")
        try:
            repo = self.github.get_repo(f"{owner}/{repo_name}")
            user = self.github.get_user()
            user_login = user.login

            has_write = False
            if owner.lower() == user_login.lower():
                has_write = True
            else:
                try:
                    permission = repo.get_collaborator_permission(user_login)
                    has_write = permission in ("admin", "write")
                except Exception:
                    has_write = False
        except Exception as e:
            raise RuntimeError(f"Failed to fetch repository metadata: {e}")

        # Fork if user lacks direct write permissions
        if not has_write:
            logger.info(
                f"User '{user_login}' does not have write access to '{owner}/{repo_name}'. Forking repository..."
            )
            try:
                fork = user.create_fork(repo)
                logger.info(
                    f"Fork requested: {fork.html_url}. Waiting for initialization..."
                )

                # Poll fork status until it is accessible
                fork_ready = False
                fork_repo_name = f"{user_login}/{repo_name}"
                for attempt in range(1, 16):
                    try:
                        fork_repo = self.github.get_repo(fork_repo_name)
                        # Test accessibility by retrieving default branch info
                        _ = fork_repo.default_branch
                        fork_ready = True
                        logger.info(
                            f"Fork is ready and accessible (attempt {attempt})."
                        )
                        break
                    except Exception:
                        logger.debug(
                            f"Fork not ready yet, retrying... (attempt {attempt}/15)"
                        )
                        time.sleep(2)

                if not fork_ready:
                    raise RuntimeError("Fork repository did not become ready in time.")

                target_repo_url = fork_repo.clone_url
                head_branch = f"{user_login}:{branch_name}"
            except Exception as e:
                raise RuntimeError(f"Failed to fork repository: {e}")
        else:
            target_repo_url = workspace.repo_url
            head_branch = branch_name

        # 2. Update git remote URL to include auth token
        logger.info("Configuring authenticated Git remote...")
        auth_url = get_auth_remote_url(target_repo_url, self.token)

        if not sandbox.use_docker:
            try:
                subprocess.run(
                    ["git", "remote", "set-url", "origin", auth_url],
                    cwd=repo_path,
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError as e:
                raise RuntimeError(
                    f"Failed to set remote URL locally: {e.stderr.decode('utf-8', errors='ignore')}"
                )
        else:
            b64_url = base64.b64encode(auth_url.encode("utf-8")).decode("utf-8")
            py_code = (
                "import base64, subprocess, sys\n"
                f"url = base64.b64decode('{b64_url}').decode('utf-8')\n"
                "try:\n"
                "    subprocess.run(['git', 'remote', 'set-url', 'origin', url], check=True, capture_output=True)\n"
                "except subprocess.CalledProcessError as e:\n"
                "    sys.stderr.write(e.stderr.decode('utf-8', errors='ignore'))\n"
                "    sys.exit(1)\n"
            )
            b64_py_code = base64.b64encode(py_code.encode("utf-8")).decode("utf-8")
            run_cmd = f"echo '{b64_py_code}' | base64 -d | python3"
            res = sandbox.exec_command(container_id, run_cmd, workdir=repo_path)
            if res.exit_code != 0:
                raise RuntimeError(
                    f"Failed to set remote URL in Docker: {res.stderr or res.stdout}"
                )

        # 3. Stage and commit changes
        logger.info("Staging and committing changes...")
        # Check if there are changes first
        if not sandbox.use_docker:
            try:
                status_res = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=repo_path,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                has_changes = bool(status_res.stdout.strip())
            except subprocess.CalledProcessError as e:
                raise RuntimeError(f"Failed to check git status locally: {e.stderr}")
        else:
            status_res = sandbox.exec_command(
                container_id, "git status --porcelain", workdir=repo_path
            )
            has_changes = bool(status_res.stdout.strip())

        if not has_changes:
            logger.warning(
                "No changes detected in repository. Skipping commit and push."
            )
            raise RuntimeError(
                "No changes to push. The issue might already be resolved or no edits were made."
            )

        commit_msg = (
            f"fix: Resolve issue #{issue_number}\n\nAutomated fix proposed by autodev."
        )

        if not sandbox.use_docker:
            try:
                subprocess.run(
                    ["git", "config", "user.name", "autodev-agent"],
                    cwd=repo_path,
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "config", "user.email", "agent@autodev.local"],
                    cwd=repo_path,
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "add", "."], cwd=repo_path, check=True, capture_output=True
                )
                subprocess.run(
                    ["git", "commit", "-m", commit_msg],
                    cwd=repo_path,
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError as e:
                raise RuntimeError(
                    f"Failed to commit changes locally: {e.stderr.decode('utf-8', errors='ignore')}"
                )
        else:
            b64_msg = base64.b64encode(commit_msg.encode("utf-8")).decode("utf-8")
            py_code = (
                "import base64, subprocess, sys\n"
                f"msg = base64.b64decode('{b64_msg}').decode('utf-8')\n"
                "try:\n"
                "    subprocess.run(['git', 'config', 'user.name', 'autodev-agent'], check=True, capture_output=True)\n"
                "    subprocess.run(['git', 'config', 'user.email', 'agent@autodev.local'], check=True, capture_output=True)\n"
                "    subprocess.run(['git', 'add', '.'], check=True, capture_output=True)\n"
                "    subprocess.run(['git', 'commit', '-m', msg], check=True, capture_output=True)\n"
                "except subprocess.CalledProcessError as e:\n"
                "    sys.stderr.write(e.stderr.decode('utf-8', errors='ignore'))\n"
                "    sys.exit(1)\n"
            )
            b64_py_code = base64.b64encode(py_code.encode("utf-8")).decode("utf-8")
            run_cmd = f"echo '{b64_py_code}' | base64 -d | python3"
            res = sandbox.exec_command(container_id, run_cmd, workdir=repo_path)
            if res.exit_code != 0:
                raise RuntimeError(
                    f"Failed to commit changes in Docker: {res.stderr or res.stdout}"
                )

        # 4. Push branch to origin
        logger.info(f"Pushing branch '{branch_name}' to remote...")
        if not sandbox.use_docker:
            try:
                subprocess.run(
                    ["git", "push", "-f", "-u", "origin", branch_name],
                    cwd=repo_path,
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError as e:
                raise RuntimeError(
                    f"Failed to push branch locally: {e.stderr.decode('utf-8', errors='ignore')}"
                )
        else:
            b64_branch = base64.b64encode(branch_name.encode("utf-8")).decode("utf-8")
            py_code = (
                "import base64, subprocess, sys\n"
                f"branch = base64.b64decode('{b64_branch}').decode('utf-8')\n"
                "try:\n"
                "    subprocess.run(['git', 'push', '-f', '-u', 'origin', branch], check=True, capture_output=True)\n"
                "except subprocess.CalledProcessError as e:\n"
                "    sys.stderr.write(e.stderr.decode('utf-8', errors='ignore'))\n"
                "    sys.exit(1)\n"
            )
            b64_py_code = base64.b64encode(py_code.encode("utf-8")).decode("utf-8")
            run_cmd = f"echo '{b64_py_code}' | base64 -d | python3"
            res = sandbox.exec_command(container_id, run_cmd, workdir=repo_path)
            if res.exit_code != 0:
                raise RuntimeError(
                    f"Failed to push branch to remote: {res.stderr or res.stdout}"
                )

        # 5. Open PR via PyGithub API
        logger.info("Opening pull request on GitHub...")
        try:
            repo = self.github.get_repo(f"{owner}/{repo_name}")

            # Default branch of the repo (e.g. main or master)
            base_branch = repo.default_branch

            pr_body = (
                f"Resolves #{issue_number}\n\n"
                f"{body}\n\n"
                f"---\n"
                f"*Opened autonomously by [autodev](https://github.com/arman1o1/autodev)*"
            )

            pr = repo.create_pull(
                title=f"autodev: {title}",
                body=pr_body,
                head=head_branch,
                base=base_branch,
            )

            logger.info(f"Successfully opened Pull Request: {pr.html_url}")
            return pr.html_url

        except GithubException as e:
            if e.status == 422:
                errors = e.data.get("errors", [])
                is_exist_err = False
                if isinstance(errors, list):
                    for err in errors:
                        if isinstance(
                            err, dict
                        ) and "A pull request already exists" in err.get("message", ""):
                            is_exist_err = True
                            break
                if is_exist_err:
                    logger.info(
                        "Pull request already exists. Retrieving and updating the existing PR..."
                    )
                    try:
                        pulls = list(repo.get_pulls(state="open", head=head_branch))
                        if pulls:
                            pr = pulls[0]
                            pr.edit(title=f"autodev: {title}", body=pr_body)
                            logger.info(
                                f"Successfully updated existing Pull Request: {pr.html_url}"
                            )
                            return pr.html_url
                    except Exception as update_err:
                        logger.error(
                            f"Failed to retrieve or update existing PR: {update_err}"
                        )
            logger.error(f"GitHub API Error creating PR: {e}")
            raise RuntimeError(
                f"Failed to open Pull Request: {e.data.get('message', str(e))}"
            ) from e
        except Exception as e:
            logger.error(f"Unexpected error creating PR: {e}")
            raise RuntimeError(f"Unexpected error opening Pull Request: {e}") from e
