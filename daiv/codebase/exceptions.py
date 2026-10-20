class MergeRequestBranchNotVisibleError(RuntimeError):
    """Raised when a just-pushed branch exists on the remote but GitLab still won't open an MR for it.

    Signals that the post-push branch-visibility race outlived the retry budget: the branch is
    confirmed present on the remote (the push landed in Gitaly), so the failure is a transient
    GitLab-side lag, not an agent-actionable error. Callers degrade to a partial "branch pushed, MR
    pending" outcome instead of failing the whole job and orphaning the run's work.
    """

    def __init__(self, source_branch: str) -> None:
        super().__init__(f"Branch '{source_branch}' was pushed but GitLab has not made it visible for MR creation yet.")
        self.source_branch = source_branch


class CloneRefNotFoundError(RuntimeError):
    """Raised when a clone fails because the requested branch/ref no longer exists on the remote.

    The common trigger is a chat session (or an MR-comment run) pinned to a merge-request branch
    that was merged and had its source branch deleted. Callers fall back to the default branch or
    skip gracefully instead of letting an opaque ``GitCommandError`` reach Sentry.
    """

    def __init__(self, ref: str, repo_slug: str) -> None:
        super().__init__(f"Ref '{ref}' does not exist on the remote for repository '{repo_slug}'.")
        self.ref = ref
        self.repo_slug = repo_slug
