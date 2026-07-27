class RepositoryRefNotFoundError(Exception):
    """Raised when a clone targets a ref the remote no longer has.

    Kept distinct from the raw ``GitCommandError`` so callers can act on "the branch is gone"
    instead of treating it as an opaque git failure. The case that motivated it: a chat session
    is pinned to the source branch of the MR it opened (``ChatSessionService.persist_ref``), and
    merging that MR with source-branch deletion — GitLab's default — leaves every later turn
    cloning a branch that no longer exists.
    """

    def __init__(self, repo_id: str, ref: str) -> None:
        super().__init__(f"Ref {ref!r} no longer exists in repository {repo_id!r}.")
        self.repo_id = repo_id
        self.ref = ref


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
