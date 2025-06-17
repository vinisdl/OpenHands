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
    BASE_VSAEX_URL: str = f'https://vsaex.dev.azure.com'
    organization: str = ''
    project: str = ''


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


        self.loadOrganization_and_project()
        self.external_auth_id = external_auth_id
        self.external_auth_token = external_auth_token

    @property
    def provider(self) -> str:
        return ProviderType.AZURE_DEVOPS.value

    @property
    def base_url(self) -> str:
        """Get the base URL for Azure DevOps API calls."""
        return f"https://{self.base_domain}"


    async def loadOrganization_and_project(self) -> None:
        if self.base_domain:
            # If base_domain is provided, it might contain organization info
            parts = self.base_domain.split('/')
            if len(parts) >= 1:
                self.organization = parts[len(parts) - 2]
            if len(parts) >= 2:
                self.project = parts[len(parts) - 1]

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
                headers = await self._get_azuredevops_headers()

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
                    headers = await self._get_azuredevops_headers()
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

    async def _get_azuredevops_headers(self) -> dict:
        """
        Retrieve the Azure DevOps Token to construct the headers
        """
        if self.user_id and not self.token:
            self.token = await self.get_latest_token()

        # Azure DevOps uses Basic authentication with PAT
        # The username is ignored (empty string), and the password is the PAT
        # Create base64 encoded credentials (username:PAT)
        credentials = base64.b64encode(
            f':{self.token.get_secret_value()}'.encode()
        ).decode()

        return {
            'Authorization': f'Basic {credentials}',
            'Content-Type': 'application/json',
        }

    async def get_user(self) -> User:
        """
        Get the authenticated user's information from Azure DevOps
        """
        headers = await self._get_azuredevops_headers()

        print(f"Azure DevOps credentials: {headers}")
        try:

            await self.loadOrganization_and_project()

            # Get the current user profile
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.BASE_VSAEX_URL}/{self.organization}/_apis/connectionData?api-version=7.2-preview.1",
                    headers=headers,
                )

            if response.status_code == 401:
                raise self.handle_http_status_error(response)
            elif response.status_code != 200:
                raise UnknownException(
                    f'Failed to get user information: {response.status_code} {response.text}'
                )

            user_data = response.json().get('authenticatedUser', {})

            # Convert string ID to integer by hashing it
            print(f"User data: {user_data}")
            user_id = hash(user_data.get('id', '')) % (2**31)
            print(f"User ID: {user_id}")

            # Create User object
            return User(
                id=user_id,
                login=user_data.get('properties.Account.$value', ''),
                avatar_url=user_data.get('imageUrl', ''),
                name=user_data.get('providerDisplayName', ''),
                email=user_data.get('properties.Account.$value', ''),
                company=None,
            )
        except httpx.RequestError as e:
            print(f"Request error: {str(e)}")
            raise UnknownException(f'Request error: {str(e)}')
        except Exception as e:
            print(f'Error: {str(e)}')


    async def get_repositories(self, sort: str, app_mode: AppMode) -> list[Repository]:
        """Get repositories for the authenticated user."""
        # Get user profile to extract organization and project
        try:
            await self.loadOrganization_and_project()
            user = await self.get_user()
            url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories"
            data, _ = await self._make_request(url)

            repositories = []
            for repo in data.get("value", []):
                repo_id = hash(repo.get('id', '')) % (2**31)
                repositories.append(
                    Repository(
                        id=repo_id,
                        full_name=f"{self.organization}/{self.project}/{repo.get('name', '')}",
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

            await self.loadOrganization_and_project()


            # Azure DevOps doesn't have a dedicated search API like GitHub
            # We'll get all repos and filter them by name
            url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories"
            data, _ = await self._make_request(url)

            repositories = []
            for repo in data.get("value", []):
                if query.lower() in repo.get("name", "").lower():
                    repo_id = hash(repo.get('id', '')) % (2**31)

                    repositories.append(
                        Repository(
                            id=repo_id,
                            full_name=f"{self.organization}/{self.project}/{repo.get('name', '')}",
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
            await self.loadOrganization_and_project()

            # Extract organization and project from base_domain

            # Get active pull requests with conflicts
            url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/pullrequests"
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
                            repo=f"{self.organization}/{self.project}/{repo_name}",
                            issue_number=pr.get("pullRequestId", 0),
                            title=pr.get("title", ""),
                        )
                    )

                # Check if PR has failing checks
                if pr.get("status") == "active" and not pr.get("isDraft", False):
                    # Get PR status
                    pr_id = pr.get("pullRequestId", 0)
                    repo_id = pr.get("repository", {}).get("id", "")
                    status_url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories/{repo_id}/pullRequests/{pr_id}/statuses"
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
                                repo=f"{self.organization}/{self.project}/{repo_name}",
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
            await self.loadOrganization_and_project()

            # Extract organization and project from base_domain or repository path
            parts = repository.split("/")
            if len(parts) >= 3:
                repo_name = parts[2]

            url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories/{repo_name}"
            data, _ = await self._make_request(url)
            repo_id = hash(data.get('id', '')) % (2**31)

            return Repository(
                id=repo_id,
                full_name=f"{self.organization}/{self.project}/{data.get('name', '')}",
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
            await self.loadOrganization_and_project()

            # Extract organization and project from base_domain or repository path
            parts = repository.split("/")
            if len(parts) >= 3:
                repo_name = parts[2]

            # First get the repository ID
            repo_url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories/{repo_name}"
            repo_data, _ = await self._make_request(repo_url)
            repo_id = repo_data.get("id", "")

            # Now get the branches
            url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories/{repo_id}/refs"
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

    async def create_pr(self, repository: str, source_branch: str, target_branch: str, title: str, body: str) -> str:
        """Create a pull request"""
        try:
            await self.loadOrganization_and_project()

            # Extract organization and project from base_domain or repository path
            parts = repository.split("/")
            if len(parts) >= 3:
                repo_name = parts[2]

            # Create the PR
            url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories/{repo_name}/pullRequests"
            payload = {
                "title": title,
                "description": body,
                "sourceRefName": source_branch,
                "targetRefName": target_branch
            }

            response, _ = await self._make_request(url, payload, RequestMethod.POST)
            return response['url']
        except Exception as e:
            logger.warning(f"Error creating pull request: {e}")
            raise UnknownException(f"Error creating pull request: {e}")


    async def create_issue(self, repository: str, title: str, body: str) -> str:
        """Create an issue"""
        try:
            await self.loadOrganization_and_project()

            # Extract organization and project from base_domain or repository path
            parts = repository.split("/")
            if len(parts) >= 3:
                repo_name = parts[2]

            # Create the task
            url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/wit/workItems"
            payload = {
                "title": title,
                "description": body,
            }

            response, _ = await self._make_request(url, payload, RequestMethod.POST)
            return response['url']

        except Exception as e:
            logger.warning(f"Error creating issue: {e}")
            raise UnknownException(f"Error creating issue: {e}")

class AzureDevOpsServiceImpl(AzureDevOpsService):
    """Implementation of the Azure DevOps service."""
    pass
