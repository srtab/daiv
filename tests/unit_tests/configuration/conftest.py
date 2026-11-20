def formset_data(prefix: str, rows: list[dict], *, initial: int = 0) -> dict[str, str]:
    """A POST dict for the formset under ``prefix``, one row per dict in ``rows``."""
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(rows)),
        f"{prefix}-INITIAL_FORMS": str(initial),
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    for index, row in enumerate(rows):
        data.update({f"{prefix}-{index}-{field}": str(value) for field, value in row.items()})
    return data
