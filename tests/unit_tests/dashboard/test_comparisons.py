from dashboard.comparisons import ASPECTS, COMPARISONS, DAIV_CELLS


def test_every_comparison_fills_exactly_the_aspects():
    keys = {key for key, _label in ASPECTS}

    assert set(DAIV_CELLS) == keys
    for comparison in COMPARISONS:
        assert set(comparison.cells) == keys, comparison.slug


def test_every_cell_cites_a_public_source():
    cells = [*DAIV_CELLS.values()] + [cell for comparison in COMPARISONS for cell in comparison.cells.values()]

    assert all(cell.sources and all(src.startswith("https://") for src in cell.sources) for cell in cells)
