import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import SecretStr

from openhands.integrations.azuredevops.azuredevops_service import AzureDevOpsServiceImpl
from openhands.integrations.service_types import ProviderType, Repository, User


@pytest.fixture
def azure_devops_service():
    return AzureDevOpsServiceImpl(
        token=SecretStr("test-token"),
        organization="test-org",
        project="test-project",
    )


@pytest.mark.asyncio
async def test_get_user(azure_devops_service):
    # Mock the _make_request method
    azure_devops_service._make_request = AsyncMock(
        return_value=(
            {
                "id": "12345",
                "displayName": "Test User",
                "emailAddress": "test@example.com",
                "avatar": {"href": "https://example.com/avatar.jpg"},
            },
            {},
        )
    )

    # Call the method
    user = await azure_devops_service.get_user()

    # Verify the result
    assert isinstance(user, User)
    assert user.id == 12345
    assert user.login == "test@example.com"
    assert user.name == "Test User"
    assert user.email == "test@example.com"
    assert user.avatar_url == "https://example.com/avatar.jpg"


@pytest.mark.asyncio
async def test_get_repositories(azure_devops_service):
    # Mock the _make_request method
    azure_devops_service._make_request = AsyncMock(
        return_value=(
            {
                "value": [
                    {
                        "id": "repo1",
                        "name": "test-repo-1",
                    },
                    {
                        "id": "repo2",
                        "name": "test-repo-2",
                    },
                ]
            },
            {},
        )
    )

    # Call the method
    repositories = await azure_devops_service.get_repositories("updated", None)

    # Verify the result
    assert len(repositories) == 2
    assert all(isinstance(repo, Repository) for repo in repositories)
    assert repositories[0].id == "repo1"
    assert repositories[0].full_name == "test-org/test-project/test-repo-1"
    assert repositories[0].git_provider == ProviderType.AZURE_DEVOPS
    assert repositories[1].id == "repo2"
    assert repositories[1].full_name == "test-org/test-project/test-repo-2"
    assert repositories[1].git_provider == ProviderType.AZURE_DEVOPS


@pytest.mark.asyncio
async def test_get_branches(azure_devops_service):
    # Mock the _make_request method for repository info
    azure_devops_service._make_request = AsyncMock()
    azure_devops_service._make_request.side_effect = [
        ({"id": "repo-id", "name": "test-repo"}, {}),  # First call for repo info
        (
            {
                "value": [
                    {
                        "name": "refs/heads/main",
                        "objectId": "commit-sha-1",
                    },
                    {
                        "name": "refs/heads/feature",
                        "objectId": "commit-sha-2",
                    },
                ]
            },
            {},
        ),  # Second call for branches
    ]

    # Call the method
    branches = await azure_devops_service.get_branches("test-org/test-project/test-repo")

    # Verify the result
    assert len(branches) == 2
    assert branches[0].name == "main"
    assert branches[0].commit_sha == "commit-sha-1"
    assert branches[1].name == "feature"
    assert branches[1].commit_sha == "commit-sha-2"