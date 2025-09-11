---
name: azuredevops
type: knowledge
version: 1.0.0
agent: CodeActAgent
triggers:
- azuredevops
- pr
- pull request
---

You have access to an environment variable, `AZURE_DEVOPS_TOKEN`, which allows you to interact with
the Azure DevOps API.

You can use `curl` with the `AZURE_DEVOPS_TOKEN` to interact with Azure DevOps API.
ALWAYS use the Azure DevOps API for operations instead of a web browser.
<<<<<<< Updated upstream
ALWAYS use the create_azuredevops_pr tool to create a pr even
ALWAYS use the comment_azure_on_pr tool to coment a pull request
=======
ALWAYS use the create_azuredevops_pr tool to create a pr
>>>>>>> Stashed changes

When interacting with the Azure DevOps API, use the `AZURE_DEVOPS_TOKEN` authentication token in your HTTP calls.

Example of an authenticated call using curl:
# Example using curl with Azure DevOps PAT
curl -H "Authorization: Basic $(echo -n ":${AZURE_DEVOPS_TOKEN}" | base64)" \
     -H "Content-Type: application/json" \
     https://dev.azure.com/{organization}/{project}/_apis/git/repositories?api-version=7.1

# Example configuring git remote with PAT
git remote set-url origin https://${AZURE_DEVOPS_TOKEN}@dev.azure.com/{organization}/{project}/_git/{repository}

Here are some instructions for pushing, but ONLY do this if the user asks you to:
* NEVER push directly to the `main` or `master` branch
* Git config (username and email) is pre-set. Do not modify.
* You may already be on a branch starting with `openhands-workspace`. Create a new branch with a better name before pushing.
* Once you've created your own branch or a pull request, continue to update it. Do NOT create a new one unless you are explicitly asked to. Update the PR title and description as necessary, but don't change the branch name.
* Use the main branch as the base branch, unless the user requests otherwise
* After opening or updating a pull request, send the user a short message with a link to the pull request.
* Prefer "Draft" pull requests when possible
* Do NOT mark a pull request as ready to review unless the user explicitly says so
