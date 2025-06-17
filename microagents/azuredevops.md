---
name: azuredevops
type: knowledge
version: 1.0.0
agent: CodeActAgent
triggers:
- azuredevops
- azure
- ado
- git
---

You have access to an environment variable, `AZURE_DEVOPS_TOKEN`, which allows you to interact with
the Azure DevOps API.

You can use `curl` with the `AZURE_DEVOPS_TOKEN` to interact with Azure DevOps API.
ALWAYS use the Azure DevOps API for operations instead of a web browser.
ALWAYS use the create_pr tool to create a pr even if it doesn't support it

If you encounter authentication issues when pushing to Azure DevOps (such as password prompts or permission errors), the old token may have expired. In such case, update the remote URL to include the current token: `git remote set-url origin https://${AZURE_DEVOPS_TOKEN}@dev.azure.com/organization/project/_git/repository`

Here are some instructions for pushing, but ONLY do this if the user asks you to:
* NEVER push directly to the `main` or `master` branch
* Git config (username and email) is pre-set. Do not modify.
* You may already be on a branch starting with `openhands-workspace`. Create a new branch with a better name before pushing.
* Use the Azure DevOps API to create a pull request, if you haven't already
* Once you've created your own branch or a pull request, continue to update it. Do NOT create a new one unless you are explicitly asked to. Update the PR title and description as necessary, but don't change the branch name.
* Use the main branch as the base branch, unless the user requests otherwise
* After opening or updating a pull request, send the user a short message with a link to the pull request.
* Prefer "Draft" pull requests when possible
* Do NOT mark a pull request as ready to review unless the user explicitly says so