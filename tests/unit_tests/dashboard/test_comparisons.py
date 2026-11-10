from dashboard.comparisons import ASPECTS, COMPARISONS, DAIV_CELLS


def test_every_comparison_fills_every_aspect():
    for comparison in COMPARISONS:
        assert [row.aspect for row in comparison.rows] == [label for _key, label in ASPECTS]


def test_every_cell_cites_a_public_source():
    cells = [*DAIV_CELLS.values()] + [cell for comparison in COMPARISONS for cell in comparison.cells.values()]

    assert all(cell.sources and all(src.startswith("https://") for src in cell.sources) for cell in cells)
