class MergeRequestBranchNotVisibleError(RuntimeError):
    """Raised when the platform still reports a pushed source branch as missing at MR-create time.

    The push succeeded, but the platform's merge-request validation kept rejecting the branch for the
    whole retry window (GitLab refreshes the branch view its ``POST /merge_requests`` checks
    asynchronously after the push; see
    :data:`~codebase.clients.gitlab.client.MERGE_REQUEST_BRANCH_VISIBILITY_RETRY_BACKOFF_SECONDS`
    for the measured lag the budget is sized from). Distinct from the raw ``GitlabCreateError`` so the publisher can
    keep the run's pushed work — reporting the branch and retrying the MR on a later turn — instead of
    failing the whole job over a transient platform delay.

    ``verified`` says whether the branch was actually *confirmed* on the remote or merely assumed
    present because the confirming read itself failed. Both keep the work, but only the confirmed case
    may be reported to the user as safe: telling someone their changes are on a branch is what stops
    them redoing the work, so an unconfirmed claim is the one direction this degradation must never
    round in. Currently GitLab-only — the GitHub client has no equivalent eventual-consistency path.
    """

    def __init__(self, source_branch: str, *, verified: bool) -> None:
        confirmation = (
            "the branch is confirmed present on the remote, so the changes are safe on it"
            if verified
            else "the branch could not be confirmed on the remote either (that check failed too), so the changes "
            "may or may not have landed"
        )
        super().__init__(
            f"The branch '{source_branch}' was pushed successfully, but the platform still reports it as "
            f"missing, so the merge request could not be opened yet — {confirmation}."
        )
        self.source_branch = source_branch
        self.verified = verified


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
