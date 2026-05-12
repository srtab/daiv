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
