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
    PaginatedBranchesResponse,
    ProviderType,
    Repository,
    RequestMethod,
    SuggestedTask,
    TaskType,
    UnknownException,
    User,
    ResourceNotFoundError,
)
from openhands.microagent.types import MicroagentContentResponse, MicroagentResponse
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

        if base_domain:
            self.base_domain = base_domain

        # Initialize organization and project from base_domain if provided
        if self.base_domain and self.base_domain != 'dev.azure.com':
            parts = self.base_domain.split('/')
            if len(parts) >= 2:
                self.organization = parts[1]  # dev.azure.com/org/project -> org
            if len(parts) >= 3:
                self.project = parts[2]  # dev.azure.com/org/project -> project

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

                if method == RequestMethod.POST:
                    response = await client.post(url, json=params, headers=headers)
                else:
                    response = await client.get(url, params=params, headers=headers)

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

    async def _get_cursorrules_url(self, repository: str) -> str:
        """Get the URL for checking .cursorrules file."""
        await self.loadOrganization_and_project()

        if not self.organization or not self.project:
            raise ResourceNotFoundError("Azure DevOps organization and project not configured.")

        # Extract repository name from repository path
        parts = repository.split("/")
        if len(parts) >= 3:
            repo_name = parts[2]
        else:
            raise ResourceNotFoundError(f"Invalid repository format: {repository}. Expected format: organization/project/repository")

        return f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories/{repo_name}/items/.cursorrules"

    async def _get_microagents_directory_url(
        self, repository: str, microagents_path: str
    ) -> str:
        """Get the URL for checking microagents directory."""
        await self.loadOrganization_and_project()

        if not self.organization or not self.project:
            raise ResourceNotFoundError("Azure DevOps organization and project not configured.")

        # Extract repository name from repository path
        parts = repository.split("/")
        if len(parts) >= 3:
            repo_name = parts[2]
        else:
            raise ResourceNotFoundError(f"Invalid repository format: {repository}. Expected format: organization/project/repository")

        return f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories/{repo_name}/items"

    def _get_microagents_directory_params(self, microagents_path: str) -> dict | None:
        """Get parameters for the microagents directory request. Return None if no parameters needed."""
        return {'path': microagents_path, 'recursionLevel': 'full'}

    def _is_valid_microagent_file(self, item: dict) -> bool:
        """Check if an item represents a valid microagent file."""
        return (
            item.get('isFolder', False) == False
            and item.get('path', '').endswith('.md')
            and not item.get('path', '').endswith('README.md')
        )

    def _get_file_name_from_item(self, item: dict) -> str:
        """Extract file name from directory item."""
        path = item.get('path', '')
        return path.split('/')[-1] if path else ''

    def _get_file_path_from_item(self, item: dict, microagents_path: str) -> str:
        """Extract file path from directory item."""
        return item.get('path', '')

    async def get_user(self) -> User:
        """
        Get the authenticated user's information from Azure DevOps
        """
        headers = await self._get_azuredevops_headers()

        print(f"Azure DevOps credentials: {headers}")
        try:
            await self.loadOrganization_and_project()

            # Try to get user info using the organization if available
            if self.organization:
                # Get the current user profile using organization
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
            else:
                # Fallback: try to get user info without organization
                # This uses a different endpoint that doesn't require organization
                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        "https://app.vssps.visualstudio.com/_apis/profile/profiles/me?api-version=6.0",
                        headers=headers,
                    )

                if response.status_code == 401:
                    raise self.handle_http_status_error(response)
                elif response.status_code != 200:
                    raise UnknownException(
                        f'Failed to get user information: {response.status_code} {response.text}'
                    )

                user_data = response.json()

            # Convert string ID to integer by hashing it
            print(f"User data: {user_data}")
            user_id = hash(user_data.get('id', '')) % (2**31)
            print(f"User ID: {user_id}")

            # Create User object
            return User(
                id=str(user_id),
                login=user_data.get('properties.Account.$value', user_data.get('emailAddress', '')),
                avatar_url=user_data.get('imageUrl', ''),
                name=user_data.get('providerDisplayName', user_data.get('displayName', '')),
                email=user_data.get('properties.Account.$value', user_data.get('emailAddress', '')),
                company=None,
            )
        except httpx.RequestError as e:
            print(f"Request error: {str(e)}")
            raise UnknownException(f'Request error: {str(e)}')
        except Exception as e:
            print(f'Error: {str(e)}')
            raise UnknownException(f'Error getting user information: {str(e)}')




    async def search_repositories(
        self,
        query: str,
        per_page: int,
        sort: str,
        order: str,
        public: bool = False,
        app_mode: AppMode = AppMode.OSS,
    ) -> list[Repository]:
        """Search for repositories."""
        try:
            await self.loadOrganization_and_project()

            # If organization and project are not set, we can't search repositories
            if not self.organization or not self.project:
                logger.warning("Azure DevOps organization and project not configured. Cannot search repositories.")
                return []

            # Azure DevOps doesn't have a dedicated search API like GitHub
            # We'll get all repos and filter them by name
            url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories"
            data, _ = await self._make_request(url)

            exact_matches = []
            partial_matches = []

            for repo in data.get("value", []):
                repo_name = repo.get("name", "")
                repo_name_lower = repo_name.lower()
                query_lower = query.lower()

                repo_id = hash(repo.get('id', '')) % (2**31)
                repository = Repository(
                    id=str(repo_id),
                    full_name=f"{self.organization}/{self.project}/{repo_name}",
                    git_provider=ProviderType.AZURE_DEVOPS,
                    is_public=False,  # Azure DevOps repos are private by default
                    stargazers_count=None,
                    pushed_at=None,
                )

                if repo_name_lower == query_lower:
                    exact_matches.append(repository)
                elif query_lower in repo_name_lower:
                    partial_matches.append(repository)

            repositories = exact_matches + partial_matches
            return repositories[:per_page]
        except Exception as e:
            logger.warning(f"Error searching repositories: {e}")
            return []

    async def get_all_repositories(
        self, sort: str, app_mode: AppMode
    ) -> list[Repository]:
        """Get all repositories for the authenticated user with pagination support."""
        try:
            await self.loadOrganization_and_project()

            # If organization and project are not set, we can't get repositories
            if not self.organization or not self.project:
                logger.warning("Azure DevOps organization and project not configured. Cannot get repositories.")
                return []

            MAX_REPOS = 1000
            all_repos: list[dict] = []
            continuation_token = None

            url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories"

            # Azure DevOps API supports pagination via continuationToken
            while len(all_repos) < MAX_REPOS:
                params = {
                    "api-version": "7.1",
                }

                # Add continuation token if available
                if continuation_token:
                    params["continuationToken"] = continuation_token

                data, headers = await self._make_request(url, params)

                if not data or not data.get("value"):
                    break

                all_repos.extend(data.get("value", []))

                # Check for continuation token in response headers
                continuation_token = headers.get("x-ms-continuationtoken")
                if not continuation_token:
                    break

            # Trim to MAX_REPOS if needed and convert to Repository objects
            all_repos = all_repos[:MAX_REPOS]
            repositories = []
            for repo in all_repos:
                repo_id = hash(repo.get('id', '')) % (2**31)
                repositories.append(
                    Repository(
                        id=str(repo_id),
                        full_name=f"{self.organization}/{self.project}/{repo.get('name', '')}",
                        git_provider=ProviderType.AZURE_DEVOPS,
                        is_public=False,  # Azure DevOps repos are private by default
                        stargazers_count=None,
                        pushed_at=None,
                    )
                )

            return repositories
        except Exception as e:
            logger.warning(f"Error getting all repositories: {e}")
            return []

    async def get_paginated_repos(
        self,
        page: int,
        per_page: int,
        sort: str,
        installation_id: str | None,
        query: str | None = None,
    ) -> list[Repository]:
        """Get a page of repositories for the authenticated user."""
        try:
            await self.loadOrganization_and_project()

            # If organization and project are not set, we can't get repositories
            if not self.organization or not self.project:
                logger.warning("Azure DevOps organization and project not configured. Cannot get repositories.")
                return []

            url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories"
            params = {
                "api-version": "7.1",
            }

            # Azure DevOps doesn't support traditional pagination like GitHub/GitLab
            # We'll get all repos and slice them to simulate pagination
            data, _ = await self._make_request(url, params)

            repositories = []
            for repo in data.get("value", []):
                repo_id = hash(repo.get('id', '')) % (2**31)
                repositories.append(
                    Repository(
                        id=str(repo_id),
                        full_name=f"{self.organization}/{self.project}/{repo.get('name', '')}",
                        git_provider=ProviderType.AZURE_DEVOPS,
                        is_public=False,  # Azure DevOps repos are private by default
                        stargazers_count=None,
                        pushed_at=None,
                    )
                )

            # Apply filtering if query is provided
            if query:
                repositories = [repo for repo in repositories if query.lower() in repo.full_name.lower()]

            # Simulate pagination by slicing the results
            start_index = (page - 1) * per_page
            end_index = start_index + per_page
            return repositories[start_index:end_index]

        except Exception as e:
            logger.warning(f"Error getting paginated repositories: {e}")
            return []

    async def get_microagents(self, repository: str) -> list[MicroagentResponse]:
        """Get microagents from a repository."""
        # Use the generic implementation from BaseGitService
        return await super().get_microagents(repository)

    async def get_microagent_content(
        self, repository: str, file_path: str
    ) -> MicroagentContentResponse:
        """Get content of a specific microagent file."""
        try:
            await self.loadOrganization_and_project()

            if not self.organization or not self.project:
                raise ResourceNotFoundError("Azure DevOps organization and project not configured.")

            # Extract repository name from repository path
            parts = repository.split("/")
            if len(parts) >= 3:
                repo_name = parts[2]
            else:
                raise ResourceNotFoundError(f"Invalid repository format: {repository}. Expected format: organization/project/repository")

            # Get the repository ID first
            repo_url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories?api-version=7.1"
            repo_data, _ = await self._make_request(repo_url)
            repo_id = None
            for repo in repo_data.get("value", []):
                if repo.get("name") == repo_name:
                    repo_id = repo.get("id")
                    break

            if not repo_id:
                raise ResourceNotFoundError(f"Repository {repo_name} not found")

            # Get the file content
            encoded_file_path = file_path.replace('/', '%2F')
            file_url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories/{repo_id}/items/{encoded_file_path}"
            params = {"api-version": "7.1"}

            response, _ = await self._make_request(file_url, params)

            # Parse the content to extract triggers from frontmatter
            return self._parse_microagent_content(response, file_path)

        except Exception as e:
            logger.warning(f"Error getting microagent content: {e}")
            raise ResourceNotFoundError(f"Error getting microagent content: {e}")

    async def get_suggested_tasks(self) -> list[SuggestedTask]:
        """Get suggested tasks for the authenticated user across all repositories."""
        try:
            await self.loadOrganization_and_project()

            # If organization and project are not set, we can't get suggested tasks
            if not self.organization or not self.project:
                logger.warning("Azure DevOps organization and project not configured. Cannot get suggested tasks.")
                return []

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

            # If organization and project are not set, we can't get repository details
            if not self.organization or not self.project:
                raise UnknownException("Azure DevOps organization and project not configured.")

            # Extract organization and project from base_domain or repository path
            parts = repository.split("/")
            if len(parts) >= 2:
                repo_name = parts[-1]
            else:
                raise UnknownException(f"Invalid repository format: {repository}. Expected format: organization/project/repository")

            url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories/{repo_name}"
            data, _ = await self._make_request(url)
            repo_id = hash(data.get('id', '')) % (2**31)

            return Repository(
                id=str(repo_id),
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

            # If organization and project are not set, we can't get branches
            if not self.organization or not self.project:
                raise UnknownException("Azure DevOps organization and project not configured.")

            # Extract organization and project from base_domain or repository path
            parts = repository.split("/")
            if len(parts) >= 2:
                repo_name = parts[-1]
            else:
                raise UnknownException(f"Invalid repository format: {repository}. Expected format: organization/project/repository")

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
                            commit_sha=str(ref.get("objectId", "")),
                            protected=False,  # Azure DevOps doesn't expose this info in the API
                            last_push_date=None,  # Azure DevOps doesn't expose this info in the API
                        )
                    )

            return branches
        except Exception as e:
            logger.warning(f"Error getting branches: {e}")
            return []

    async def get_paginated_branches(
        self, repository: str, page: int = 1, per_page: int = 30
    ) -> PaginatedBranchesResponse:
        """Get branches for a repository with pagination"""
        try:
            # Get all branches first
            all_branches = await self.get_branches(repository)

            # Calculate pagination
            total_count = len(all_branches)
            start_index = (page - 1) * per_page
            end_index = start_index + per_page

            # Slice the branches for the current page
            paginated_branches = all_branches[start_index:end_index]

            # Check if there's a next page
            has_next_page = end_index < total_count

            return PaginatedBranchesResponse(
                branches=paginated_branches,
                has_next_page=has_next_page,
                current_page=page,
                per_page=per_page,
                total_count=total_count,
            )
        except Exception as e:
            logger.warning(f"Error getting paginated branches: {e}")
            # Return empty response on error
            return PaginatedBranchesResponse(
                branches=[],
                has_next_page=False,
                current_page=page,
                per_page=per_page,
                total_count=0,
            )

    async def search_branches(
        self, repository: str, query: str, per_page: int = 30
    ) -> list[Branch]:
        """Search branches by name. Azure DevOps API doesn't support search, so we filter locally."""
        try:
            # Get all branches first
            all_branches = await self.get_branches(repository)

            # Filter branches by query (case-insensitive)
            query_lower = query.lower()
            filtered_branches = [
                branch
                for branch in all_branches
                if query_lower in branch.name.lower()
            ]

            # Limit results to per_page
            return filtered_branches[:per_page]
        except Exception as e:
            logger.warning(f"Error searching branches: {e}")
            return []

    async def create_pr(self, repository: str, source_branch: str, target_branch: str, title: str, body: str) -> str:
        """Create a pull request"""
        try:
            await self.loadOrganization_and_project()

            # If organization and project are not set, we can't create PR
            if not self.organization or not self.project:
                raise UnknownException("Azure DevOps organization and project not configured.")

            print(f"base_domain: {self.base_domain}")
            print(f"organization: {self.organization}")
            print(f"project: {self.project}")
            print(f"repository: {repository}")
            print(f"source_branch: {source_branch}")
            print(f"target_branch: {target_branch}")
            print(f"title: {title}")
            print(f"body: {body}")

            # Extract repository name from repository path
            parts = repository.split("/")
            if len(parts) >= 2:
                repo_name = parts[-1]  # organization/project/repository -> repository
            else:
                raise UnknownException(f"Invalid repository format: {repository}. Expected format: organization/project/repository")

            # Buscar o repository ID pelo nome
            repo_url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories?api-version=7.1"
            repo_data, _ = await self._make_request(repo_url)
            repo_id = None
            for repo in repo_data.get("value", []):
                if repo.get("name") == repo_name:
                    repo_id = repo.get("id")
                    break
            if not repo_id:
                raise UnknownException(f"Repository {repo_name} not found")

            # Create the payload for the pull request
            payload = {
                "title": title,
                "description": body or "",
                "sourceRefName": f"refs/heads/{source_branch}",
                "targetRefName": f"refs/heads/{target_branch}"
            }
            print(f"Payload sent for PR: {payload}")

            # Send the POST with the payload as JSON
            url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories/{repo_id}/pullRequests?api-version=7.1"
            response, _ = await self._make_request(url, payload, RequestMethod.POST)
            return response.get('url', '')
        except Exception as e:
            print(f"Error creating pull request: {e}")
            logger.warning(f"Error creating pull request: {e}")
            raise UnknownException(f"Error creating pull request: {e}")


    async def create_issue(self, repository: str, title: str, body: str) -> str:
        """Create an issue"""
        try:
            await self.loadOrganization_and_project()

            # If organization and project are not set, we can't create issue
            if not self.organization or not self.project:
                raise UnknownException("Azure DevOps organization and project not configured.")

            # Extract organization and project from base_domain or repository path
            parts = repository.split("/")
            if len(parts) >= 2:
                repo_name = parts[-1]
            else:
                raise UnknownException(f"Invalid repository format: {repository}. Expected format: organization/project/repository")

            # Create the work item
            url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/wit/workItems"
            payload = {
                "title": title,
                "description": body,
            }

            response, _ = await self._make_request(url, payload, RequestMethod.POST)
            return response.get('url', '')

        except Exception as e:
            logger.warning(f"Error creating issue: {e}")
            raise UnknownException(f"Error creating issue: {e}")


    async def comment_on_pr(self, repository: str, pr_number: int, comment: str) -> str:
        """Adiciona um comentário em um pull request"""
        try:
            await self.loadOrganization_and_project()

            # Se organização e projeto não estiverem configurados, não podemos comentar
            if not self.organization or not self.project:
                raise UnknownException("Organização e projeto do Azure DevOps não configurados.")

            # Extrai organização e projeto do caminho do repositório
            parts = repository.split("/")
            if len(parts) >= 2:
                repo_name = parts[-1]
            else:
                raise UnknownException(f"Formato de repositório inválido: {repository}. Formato esperado: organization/project/repository")

            # Cria o comentário no pull request
            url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories/{repo_name}/pullRequests/{pr_number}/threads?api-version=7.1"

            payload = {
                "comments": [
                    {
                        "content": comment
                    }
                ]
            }

            response, _ = await self._make_request(url, payload, RequestMethod.POST)
            return response.get('url', '')

        except Exception as e:
            logger.warning(f"Erro ao comentar no pull request: {e}")
            raise UnknownException(f"Erro ao comentar no pull request: {e}")


class AzureDevOpsServiceImpl(AzureDevOpsService):
    """Implementation of the Azure DevOps service."""
    pass
