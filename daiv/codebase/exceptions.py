class SingleRepoRequiredError(RuntimeError):
    """Raised when ``RuntimeCtx.repo`` is accessed without exactly one handle.

    ``actual`` distinguishes repoless (``0``) from multi-repo (``>= 2``).
    """

    def __init__(self, actual: int) -> None:
        super().__init__(f"RuntimeCtx.repo requires exactly one repository handle, got {actual}.")
        self.actual = actual


class InvalidThreadResumeError(RuntimeError):
    """Raised when a job is enqueued with a ``thread_id`` that is already bound to
    a different repo-binding mode (repoless ↔ repo-bound)."""

    def __init__(self, thread_id: str, expected: str | None, got: str | None) -> None:
        super().__init__(
            f"Thread {thread_id!r} was previously bound to repo_id={expected!r}; cannot resume with repo_id={got!r}."
        )
        self.thread_id = thread_id
        self.expected = expected
        self.got = got
