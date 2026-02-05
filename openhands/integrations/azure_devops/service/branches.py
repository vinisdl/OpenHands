"""Branch operations for Azure DevOps integration."""

from openhands.core.logger import openhands_logger as logger
from openhands.integrations.azure_devops.service.base import AzureDevOpsMixinBase
from openhands.integrations.service_types import (
    Branch,
    PaginatedBranchesResponse,
    ResourceNotFoundError,
)


class AzureDevOpsBranchesMixin(AzureDevOpsMixinBase):
    """Mixin for Azure DevOps branch operations."""

    async def get_branches(self, repository: str) -> list[Branch]:
        """Get branches for a repository."""
        org_enc, project_enc, repo_enc = self._get_encoded_repo_components(repository)

        # Try to get branches directly using the repository name
        # Azure DevOps API accepts both name and ID for repository identifier
        # Pass query parameters via params to avoid double encoding
        url = f'https://dev.azure.com/{org_enc}/{project_enc}/_apis/git/repositories/{repo_enc}/refs'
        params = {'api-version': '7.1', 'filter': 'heads/'}

        # Set maximum branches to fetch
        MAX_BRANCHES = 1000

        try:
            response, _ = await self._make_request(url, params=params)
            branches_data = response.get('value', [])

            all_branches = []

            for branch_data in branches_data:
                # Extract branch name from the ref (e.g., "refs/heads/main" -> "main")
                name = branch_data.get('name', '').replace('refs/heads/', '')

                # Get the commit details for this branch
                object_id = branch_data.get('objectId', '')
                commit_url = f'https://dev.azure.com/{org_enc}/{project_enc}/_apis/git/repositories/{repo_enc}/commits/{object_id}'
                commit_params = {'api-version': '7.1'}
                try:
                    commit_data, _ = await self._make_request(commit_url, params=commit_params)
                    last_push_date = commit_data.get('committer', {}).get('date')
                except Exception:
                    last_push_date = None

                # Check if the branch is protected (skip if we can't get repo ID)
                is_protected = False
                try:
                    # Try to get repo ID for policy check
                    repo_url = f'https://dev.azure.com/{org_enc}/{project_enc}/_apis/git/repositories/{repo_enc}'
                    repo_params = {'api-version': '7.1'}
                    repo_data, _ = await self._make_request(repo_url, params=repo_params)
                    repo_id = repo_data.get('id')
                    if repo_id:
                        name_enc = self._encode_url_component(name)
                        policy_url = f'https://dev.azure.com/{org_enc}/{project_enc}/_apis/git/policy/configurations'
                        policy_params = {
                            'api-version': '7.1',
                            'repositoryId': repo_id,
                            'refName': f'refs/heads/{name_enc}',
                        }
                        policy_data, _ = await self._make_request(policy_url, params=policy_params)
                        is_protected = len(policy_data.get('value', [])) > 0
                except Exception:
                    # If policy check fails, assume not protected
                    is_protected = False

                branch = Branch(
                    name=name,
                    commit_sha=object_id,
                    protected=is_protected,
                    last_push_date=last_push_date,
                )
                all_branches.append(branch)

                if len(all_branches) >= MAX_BRANCHES:
                    break

            return all_branches
        except ResourceNotFoundError:
            # Re-raise ResourceNotFoundError so it can be handled by the caller
            raise
        except Exception:
            # For other errors, return empty list
            # The error will be logged by the caller
            return []

    async def get_paginated_branches(
        self, repository: str, page: int = 1, per_page: int = 30
    ) -> PaginatedBranchesResponse:
        """Get branches for a repository with pagination."""
        logger.info(
            f'[Azure DevOps Branches] Getting branches for repository: {repository}'
        )
        org_enc, project_enc, repo_enc = self._get_encoded_repo_components(repository)

        # Log encoding details
        org, project, repo = self._parse_repository(repository)
        logger.debug(
            f'[Azure DevOps Branches] Parsed repository - org: {org}, project: {project}, repo: {repo}'
        )
        logger.debug(
            f'[Azure DevOps Branches] Encoded components - org: {org_enc}, project: {project_enc}, repo: {repo_enc}'
        )

        # Try to get branches directly using the repository name
        # Azure DevOps API accepts both name and ID for repository identifier
        # Pass query parameters via params to avoid double encoding
        url = f'https://dev.azure.com/{org_enc}/{project_enc}/_apis/git/repositories/{repo_enc}/refs'
        params = {'api-version': '7.1', 'filter': 'heads/'}
        logger.debug(
            f'[Azure DevOps Branches] Constructed URL: {url}, Params: {params}'
        )

        try:
            response, _ = await self._make_request(url, params=params)
            branches_data = response.get('value', [])

            # Calculate pagination
            start_idx = (page - 1) * per_page
            end_idx = start_idx + per_page
            paginated_data = branches_data[start_idx:end_idx]

            branches: list[Branch] = []
            for branch_data in paginated_data:
                # Extract branch name from the ref (e.g., "refs/heads/main" -> "main")
                name = branch_data.get('name', '').replace('refs/heads/', '')

                # Get the commit details for this branch
                object_id = branch_data.get('objectId', '')
                commit_url = f'https://dev.azure.com/{org_enc}/{project_enc}/_apis/git/repositories/{repo_enc}/commits/{object_id}'
                commit_params = {'api-version': '7.1'}
                try:
                    commit_data, _ = await self._make_request(commit_url, params=commit_params)
                    last_push_date = commit_data.get('committer', {}).get('date')
                except Exception:
                    last_push_date = None

                # Check if the branch is protected (skip if we can't get repo ID)
                is_protected = False
                try:
                    # Try to get repo ID for policy check
                    repo_url = f'https://dev.azure.com/{org_enc}/{project_enc}/_apis/git/repositories/{repo_enc}'
                    repo_params = {'api-version': '7.1'}
                    repo_data, _ = await self._make_request(repo_url, params=repo_params)
                    repo_id = repo_data.get('id')
                    if repo_id:
                        name_enc = self._encode_url_component(name)
                        policy_url = f'https://dev.azure.com/{org_enc}/{project_enc}/_apis/git/policy/configurations'
                        policy_params = {
                            'api-version': '7.1',
                            'repositoryId': repo_id,
                            'refName': f'refs/heads/{name_enc}',
                        }
                        policy_data, _ = await self._make_request(policy_url, params=policy_params)
                        is_protected = len(policy_data.get('value', [])) > 0
                except Exception:
                    # If policy check fails, assume not protected
                    is_protected = False

                branch = Branch(
                    name=name,
                    commit_sha=object_id,
                    protected=is_protected,
                    last_push_date=last_push_date,
                )
                branches.append(branch)

            # Determine if there's a next page
            has_next_page = end_idx < len(branches_data)

            return PaginatedBranchesResponse(
                branches=branches,
                has_next_page=has_next_page,
                current_page=page,
                per_page=per_page,
                total_count=len(branches_data),
            )
        except ResourceNotFoundError as e:
            # Log detailed error before re-raising
            logger.error(
                f'[Azure DevOps Branches] ResourceNotFoundError for repository {repository}: {str(e)}'
            )
            logger.error(
                f'[Azure DevOps Branches] Failed URL: {url}, Params: {params}'
            )
            # Re-raise ResourceNotFoundError so it can be handled by the caller
            raise
        except Exception as e:
            # Log detailed error
            logger.error(
                f'[Azure DevOps Branches] Unexpected error for repository {repository}: {type(e).__name__}: {str(e)}'
            )
            logger.error(
                f'[Azure DevOps Branches] Failed URL: {url}, Params: {params}'
            )
            # For other errors, return empty response
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
        """Search for branches within a repository."""
        org_enc, project_enc, repo_enc = self._get_encoded_repo_components(repository)

        # Try to get branches directly using the repository name
        # Pass query parameters via params to avoid double encoding
        url = f'https://dev.azure.com/{org_enc}/{project_enc}/_apis/git/repositories/{repo_enc}/refs'
        params = {'api-version': '7.1', 'filter': 'heads/'}

        try:
            response, _ = await self._make_request(url, params=params)
            branches_data = response.get('value', [])

            # Filter branches by query
            filtered_branches = []
            for branch_data in branches_data:
                # Extract branch name from the ref (e.g., "refs/heads/main" -> "main")
                name = branch_data.get('name', '').replace('refs/heads/', '')

                # Check if query matches branch name
                if query.lower() in name.lower():
                    object_id = branch_data.get('objectId', '')

                    # Get commit details for this branch
                    commit_url = f'https://dev.azure.com/{org_enc}/{project_enc}/_apis/git/repositories/{repo_enc}/commits/{object_id}?api-version=7.1'
                    try:
                        commit_data, _ = await self._make_request(commit_url)
                        last_push_date = commit_data.get('committer', {}).get('date')
                    except Exception:
                        last_push_date = None

                    branch = Branch(
                        name=name,
                        commit_sha=object_id,
                        protected=False,  # Skip protected check for search to improve performance
                        last_push_date=last_push_date,
                    )
                    filtered_branches.append(branch)

                    if len(filtered_branches) >= per_page:
                        break

            return filtered_branches
        except ResourceNotFoundError:
            # Re-raise ResourceNotFoundError so it can be handled by the caller
            raise
        except Exception:
            # For other errors, return empty list
            return []
