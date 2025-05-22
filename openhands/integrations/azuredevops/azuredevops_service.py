import base64
import os
from typing import Any

import httpx
from pydantic import SecretStr

from openhands.core.logger import openhands_logger as logger
from openhands.integrations.service_types import (
    BaseGitService,
    Branch,
    GitService,
    ProviderType,
    Repository,
    RequestMethod,
    SuggestedTask,
    TaskType,
    UnknownException,
    User,
)
from openhands.server.types import AppMode
from openhands.utils.import_utils import get_impl


class AzureDevOpsService(BaseGitService, GitService):
    token: SecretStr = SecretStr('')
    refresh = False
    base_domain: str = 'dev.azure.com'

    def __init__(
        self,
        user_id: str | None = None,
        external_auth_id: str | None = None,
        external_auth_token: SecretStr | None = None,
        token: SecretStr | None = None,
        external_token_manager: bool = False,
        base_domain: str | None = None,
    ):
        self.user_id = user_id
        self.external_token_manager = external_token_manager

        if token:
            self.token = token
        
        if base_domain:
            self.base_domain = base_domain

        self.external_auth_id = external_auth_id
        self.external_auth_token = external_auth_token

    @property
    def provider(self) -> str:
        return ProviderType.AZURE_DEVOPS.value

    @property
    def base_url(self) -> str:
        """Get the base URL for Azure DevOps API calls."""
        return f"https://{self.base_domain}"

    async def _get_azure_devops_headers(self) -> dict:
        """Retrieve the Azure DevOps PAT token to construct the headers."""
        if not self.token:
            self.token = await self.get_latest_token()

        # Azure DevOps uses Basic Auth with PAT token as password
        auth_str = f":{self.token.get_secret_value() if self.token else ''}"
        encoded_auth = base64.b64encode(auth_str.encode()).decode()
        
        return {
            'Authorization': f'Basic {encoded_auth}',
            'Content-Type': 'application/json',
            'Accept': 'application/json;api-version=7.0',
        }

    def _has_token_expired(self, status_code: int) -> bool:
        return status_code == 401

    async def get_latest_token(self) -> SecretStr | None:
        return self.token

    async def _make_request(
        self,
        url: str,
        params: dict | None = None,
        method: RequestMethod = RequestMethod.GET,
    ) -> tuple[Any, dict]:
        try:
            async with httpx.AsyncClient() as client:
                headers = await self._get_azure_devops_headers()

                # Make initial request
                response = await self.execute_request(
                    client=client,
                    url=url,
                    headers=headers,
                    params=params,
                    method=method,
                )

                # Handle token refresh if needed
                if self.refresh and self._has_token_expired(response.status_code):
                    await self.get_latest_token()
                    headers = await self._get_azure_devops_headers()
                    response = await self.execute_request(
                        client=client,
                        url=url,
                        headers=headers,
                        params=params,
                        method=method,
                    )

                response.raise_for_status()
                data = response.json()
                return data, response.headers

        except httpx.HTTPStatusError as e:
            raise self.handle_http_status_error(e)
        except httpx.HTTPError as e:
            raise self.handle_http_error(e)
        except Exception as e:
            logger.warning(f"Error making request to Azure DevOps API: {e}")
            raise UnknownException(f"Unknown error: {e}")

    async def get_user(self) -> User:
        """Get the authenticated user's information."""
        url = f"{self.base_url}/_apis/profile/profiles/me"
        data, _ = await self._make_request(url)
        
        # Azure DevOps API returns different user structure than GitHub/GitLab
        return User(
            id=data.get("id", 0),
            login=data.get("emailAddress", ""),
            avatar_url=data.get("avatar", {}).get("href", ""),
            name=data.get("displayName", ""),
            email=data.get("emailAddress", ""),
            company=None,
        )

    async def get_repositories(self, sort: str, app_mode: AppMode) -> list[Repository]:
        """Get repositories for the authenticated user."""
        # Get user profile to extract organization and project
        try:
            user = await self.get_user()
            
            # Extract organization and project from base_domain
            parts = self.base_domain.split('/')
            if len(parts) >= 3:
                organization = parts[1]
                project = parts[2]
            else:
                # If we can't extract from base_domain, we can't proceed
                return []
                
            url = f"https://dev.azure.com/{organization}/{project}/_apis/git/repositories"
            data, _ = await self._make_request(url)
            
            repositories = []
            for repo in data.get("value", []):
                repositories.append(
                    Repository(
                        id=repo.get("id", 0),
                        full_name=f"{organization}/{project}/{repo.get('name', '')}",
                        git_provider=ProviderType.AZURE_DEVOPS,
                        is_public=False,  # Azure DevOps repos are private by default
                        stargazers_count=None,
                        pushed_at=None,
                    )
                )
            
            return repositories
        except Exception as e:
            logger.warning(f"Error getting repositories: {e}")
            return []

    async def search_repositories(
        self,
        query: str,
        per_page: int,
        sort: str,
        order: str,
    ) -> list[Repository]:
        """Search for repositories."""
        try:
            # Extract organization and project from base_domain
            parts = self.base_domain.split('/')
            if len(parts) >= 3:
                organization = parts[1]
                project = parts[2]
            else:
                # If we can't extract from base_domain, we can't proceed
                return []
                
            # Azure DevOps doesn't have a dedicated search API like GitHub
            # We'll get all repos and filter them by name
            url = f"https://dev.azure.com/{organization}/{project}/_apis/git/repositories"
            data, _ = await self._make_request(url)
            
            repositories = []
            for repo in data.get("value", []):
                if query.lower() in repo.get("name", "").lower():
                    repositories.append(
                        Repository(
                            id=repo.get("id", 0),
                            full_name=f"{organization}/{project}/{repo.get('name', '')}",
                            git_provider=ProviderType.AZURE_DEVOPS,
                            is_public=False,  # Azure DevOps repos are private by default
                            stargazers_count=None,
                            pushed_at=None,
                        )
                    )
            
            return repositories[:per_page]
        except Exception as e:
            logger.warning(f"Error searching repositories: {e}")
            return []

    async def get_suggested_tasks(self) -> list[SuggestedTask]:
        """Get suggested tasks for the authenticated user across all repositories."""
        try:
            # Extract organization and project from base_domain
            parts = self.base_domain.split('/')
            if len(parts) >= 3:
                organization = parts[1]
                project = parts[2]
            else:
                # If we can't extract from base_domain, we can't proceed
                return []
                
            # Get active pull requests with conflicts
            url = f"https://dev.azure.com/{organization}/{project}/_apis/git/pullrequests"
            params = {"status": "active"}
            data, _ = await self._make_request(url, params)
            
            tasks = []
            for pr in data.get("value", []):
                # Check if PR has merge conflicts
                if pr.get("mergeStatus") == "conflicts":
                    repo_name = pr.get("repository", {}).get("name", "")
                    tasks.append(
                        SuggestedTask(
                            git_provider=ProviderType.AZURE_DEVOPS,
                            task_type=TaskType.MERGE_CONFLICTS,
                            repo=f"{organization}/{project}/{repo_name}",
                            issue_number=pr.get("pullRequestId", 0),
                            title=pr.get("title", ""),
                        )
                    )
                
                # Check if PR has failing checks
                if pr.get("status") == "active" and not pr.get("isDraft", False):
                    # Get PR status
                    pr_id = pr.get("pullRequestId", 0)
                    repo_id = pr.get("repository", {}).get("id", "")
                    status_url = f"https://dev.azure.com/{organization}/{project}/_apis/git/repositories/{repo_id}/pullRequests/{pr_id}/statuses"
                    status_data, _ = await self._make_request(status_url)
                    
                    has_failing_checks = False
                    for status in status_data.get("value", []):
                        if status.get("state") == "failed":
                            has_failing_checks = True
                            break
                    
                    if has_failing_checks:
                        repo_name = pr.get("repository", {}).get("name", "")
                        tasks.append(
                            SuggestedTask(
                                git_provider=ProviderType.AZURE_DEVOPS,
                                task_type=TaskType.FAILING_CHECKS,
                                repo=f"{organization}/{project}/{repo_name}",
                                issue_number=pr_id,
                                title=pr.get("title", ""),
                            )
                        )
            
            return tasks
        except Exception as e:
            logger.warning(f"Error getting suggested tasks: {e}")
            return []

    async def get_repository_details_from_repo_name(self, repository: str) -> Repository:
        """Gets all repository details from repository name."""
        try:
            # Extract organization and project from base_domain or repository path
            parts = repository.split("/")
            if len(parts) >= 3:
                org = parts[0]
                proj = parts[1]
                repo_name = parts[2]
            else:
                # Extract from base_domain
                domain_parts = self.base_domain.split('/')
                if len(domain_parts) >= 3:
                    org = domain_parts[1]
                    proj = domain_parts[2]
                    repo_name = parts[-1]
                else:
                    raise UnknownException("Cannot determine organization and project")
                
            url = f"https://dev.azure.com/{org}/{proj}/_apis/git/repositories/{repo_name}"
            data, _ = await self._make_request(url)
            
            return Repository(
                id=data.get("id", 0),
                full_name=f"{org}/{proj}/{data.get('name', '')}",
                git_provider=ProviderType.AZURE_DEVOPS,
                is_public=False,  # Azure DevOps repos are private by default
                stargazers_count=None,
                pushed_at=None,
            )
        except Exception as e:
            logger.warning(f"Error getting repository details: {e}")
            raise UnknownException(f"Error getting repository details: {e}")

    async def get_branches(self, repository: str) -> list[Branch]:
        """Get branches for a repository."""
        try:
            # Extract organization and project from base_domain or repository path
            parts = repository.split("/")
            if len(parts) >= 3:
                org = parts[0]
                proj = parts[1]
                repo_name = parts[2]
            else:
                # Extract from base_domain
                domain_parts = self.base_domain.split('/')
                if len(domain_parts) >= 3:
                    org = domain_parts[1]
                    proj = domain_parts[2]
                    repo_name = parts[-1]
                else:
                    return []
                
            # First get the repository ID
            repo_url = f"https://dev.azure.com/{org}/{proj}/_apis/git/repositories/{repo_name}"
            repo_data, _ = await self._make_request(repo_url)
            repo_id = repo_data.get("id", "")
            
            # Now get the branches
            url = f"https://dev.azure.com/{org}/{proj}/_apis/git/repositories/{repo_id}/refs"
            params = {"filter": "heads/"}
            data, _ = await self._make_request(url, params)
            
            branches = []
            for ref in data.get("value", []):
                if ref.get("name", "").startswith("refs/heads/"):
                    branch_name = ref.get("name", "").replace("refs/heads/", "")
                    branches.append(
                        Branch(
                            name=branch_name,
                            commit_sha=ref.get("objectId", ""),
                            protected=False,  # Azure DevOps doesn't expose this info in the API
                            last_push_date=None,  # Azure DevOps doesn't expose this info in the API
                        )
                    )
            
            return branches
        except Exception as e:
            logger.warning(f"Error getting branches: {e}")
            return []


class AzureDevOpsServiceImpl(AzureDevOpsService):
    """Implementation of the Azure DevOps service."""
    pass