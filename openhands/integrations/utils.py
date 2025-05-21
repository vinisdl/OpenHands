from pydantic import SecretStr

from openhands.integrations.azuredevops.azuredevops_service import AzureDevOpsService
from openhands.integrations.github.github_service import GitHubService
from openhands.integrations.gitlab.gitlab_service import GitLabService
from openhands.integrations.provider import ProviderType


async def validate_provider_token(
    token: SecretStr, base_domain: str | None = None
) -> ProviderType | None:
    """
    Determine whether a token is for GitHub, GitLab, or Azure DevOps by attempting to get user info
    from the services.

    Args:
        token: The token to check
        base_domain: Optional base domain for the service

    Returns:
        'github' if it's a GitHub token
        'gitlab' if it's a GitLab token
        'azure_devops' if it's an Azure DevOps token
        None if the token is invalid for all services
    """
    # Try GitHub first
    try:
        github_service = GitHubService(token=token, base_domain=base_domain)
        await github_service.verify_access()
        return ProviderType.GITHUB
    except Exception:
        pass

    # Try GitLab next
    try:
        gitlab_service = GitLabService(token=token, base_domain=base_domain)
        await gitlab_service.get_user()
        return ProviderType.GITLAB
    except Exception:
        pass

    # Try Azure DevOps last
    try:
        # For Azure DevOps, we need organization and project
        # These would typically be provided in the ProviderToken
        # but for validation we just check if the token works
        azure_service = AzureDevOpsService(token=token)
        if base_domain:
            # If base_domain is provided, it might contain organization info
            parts = base_domain.split('/')
            if len(parts) >= 1:
                azure_service.organization = parts[0]
            if len(parts) >= 2:
                azure_service.project = parts[1]

        # If we have organization set, try to get user info
        if azure_service.organization:
            await azure_service.get_user()
            return ProviderType.AZURE_DEVOPS
    except Exception:
        pass

    return None
