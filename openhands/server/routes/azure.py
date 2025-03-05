from fastapi import APIRouter, HTTPException, Query
import httpx

router = APIRouter()

@router.get("/api/azure/repositories")
async def get_azure_repositories(
    pat: str = Query(..., description="Azure DevOps Personal Access Token"),
    org: str = Query(..., description="Azure DevOps Organization name"),
    project: str = Query(None, description="Azure DevOps project name (optional)")
):
    auth = ("", pat)
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(auth=auth, headers=headers) as client:
        if project:
            url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories?api-version=6.0"
            response = await client.get(url)
            try:
                response.raise_for_status()
            except httpx.HTTPError as e:
                raise HTTPException(status_code=response.status_code, detail=str(e))
            data = response.json()
            return {"repositories": data.get("value", [])}
        else:
            # List all projects first
            projects_url = f"https://dev.azure.com/{org}/_apis/projects?api-version=6.0"
            projects_resp = await client.get(projects_url)
            try:
                projects_resp.raise_for_status()
            except httpx.HTTPError as e:
                raise HTTPException(status_code=projects_resp.status_code, detail=str(e))
            projects_data = projects_resp.json()
            projects = projects_data.get("value", [])
            all_repos = []
            for proj in projects:
                proj_name = proj.get("name")
                if proj_name:
                    repos_url = f"https://dev.azure.com/{org}/{proj_name}/_apis/git/repositories?api-version=6.0"
                    repos_resp = await client.get(repos_url)
                    if repos_resp.status_code == 200:
                        repos_data = repos_resp.json()
                        repos = repos_data.get("value", [])
                        for repo in repos:
                            repo["project"] = proj_name
                        all_repos.extend(repos)
            return {"repositories": all_repos}
