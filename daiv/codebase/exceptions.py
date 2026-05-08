class SingleRepoRequiredError(RuntimeError):
    """Raised when ``RuntimeCtx.repo`` is accessed on a context that does not hold
    exactly one repository handle.

    The ``actual`` count distinguishes the repoless case (``0``) from the
    multi-repo case (``>= 2``). Tools that catch this can render a tailored
    error to the LLM.
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
