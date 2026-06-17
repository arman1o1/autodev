import logging
import re
from typing import Tuple, Optional
from pydantic import BaseModel
from github import Github, Auth
from github.GithubException import GithubException

logger = logging.getLogger("autodev.github.issues")


class GithubIssue(BaseModel):
    owner: str
    repo: str
    number: int
    title: str
    body: str
    repo_url: str


def parse_issue_url(url: str) -> Tuple[str, str, int]:
    """Parses a GitHub issue URL into owner, repo name, and issue number.

    Example: https://github.com/owner/repo/issues/123 -> ('owner', 'repo', 123)
    """
    pattern = r"github\.com/([^/]+)/([^/]+)/issues/(\d+)"
    match = re.search(pattern, url)
    if not match:
        raise ValueError(
            f"Invalid GitHub issue URL format: '{url}'. Expected format: https://github.com/owner/repo/issues/number"
        )
    owner, repo, number = match.groups()
    return owner, repo, int(number)


class IssueFetcher:
    def __init__(self, token: Optional[str] = None):
        # Initialize GitHub client
        if token:
            self.github = Github(auth=Auth.Token(token))
        else:
            self.github = Github()

    def fetch_issue(self, url: str) -> GithubIssue:
        """Fetches issue details from the GitHub API."""
        try:
            owner, repo_name, number = parse_issue_url(url)
            logger.info(f"Fetching issue #{number} from {owner}/{repo_name}...")

            repo = self.github.get_repo(f"{owner}/{repo_name}")
            issue = repo.get_issue(number)

            return GithubIssue(
                owner=owner,
                repo=repo_name,
                number=number,
                title=issue.title,
                body=issue.body or "",
                # SSH or HTTPS URL. We default to HTTPS clone URL
                repo_url=repo.clone_url,
            )
        except GithubException as e:
            logger.error(f"GitHub API error fetching issue: {e}")
            error_data = getattr(e, "data", None) or {}
            raise RuntimeError(
                f"Failed to fetch issue from GitHub: {error_data.get('message', str(e))}"
            ) from e
        except Exception as e:
            logger.error(f"Error fetching issue details: {e}")
            raise RuntimeError(f"Error resolving issue URL: {e}") from e
