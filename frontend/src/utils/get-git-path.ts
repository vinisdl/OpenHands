/**
 * Get the git repository path for a conversation.
 * Must match backend logic: clone uses split('/')[-1] as directory name
 * (see app_conversation_service_base.clone_or_init_git_repo).
 *
 * If a repository is selected, returns /workspace/project/{repo-name}
 * Otherwise, returns /workspace/project
 *
 * @param selectedRepository The selected repository (e.g., "OpenHands/OpenHands", "owner/repo", or "org/project/repo")
 * @returns The git path to use
 */
export function getGitPath(
  selectedRepository: string | null | undefined,
): string {
  if (!selectedRepository) {
    return "/workspace/project";
  }

  const parts = selectedRepository.split("/").filter(Boolean);
  const repoName = parts.length > 0 ? parts[parts.length - 1] : "";

  return repoName ? `/workspace/project/${repoName}` : "/workspace/project";
}
