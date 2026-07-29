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


class SingleRepoRequiredError(RuntimeError):
    """Raised when a ``RuntimeCtx`` is constructed or accessed without exactly one repo handle.

    Today every run is enforced to be single-repo; the error guards the multi-repo seam.
    ``actual`` is the number of handles supplied — ``0`` means the caller forgot to
    supply one (likely a misuse), ``>= 2`` means multi-repo, which isn't supported yet.
    """

    def __init__(self, actual: int) -> None:
        detail = "got 0 (no repository supplied)" if actual == 0 else f"got {actual} (multi-repo not yet supported)"
        super().__init__(f"RuntimeCtx requires exactly one repository handle, {detail}.")
        self.actual = actual
