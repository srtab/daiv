from decimal import Decimal

import pytest
from activity.templatetags.activity_tags import format_cost, format_tokens


class TestFormatCost:
    def test_none_returns_empty(self):
        assert format_cost(None) == ""

    def test_sub_cent_shows_four_decimals(self):
        assert format_cost(Decimal("0.003")) == "$0.0030"

    def test_above_cent_shows_two_decimals(self):
        assert format_cost(Decimal("1.50")) == "$1.50"

    def test_exact_cent_boundary(self):
        assert format_cost(Decimal("0.01")) == "$0.01"

    def test_string_input(self):
        assert format_cost("0.005") == "$0.0050"


class TestFormatTokens:
    def test_none_returns_empty(self):
        assert format_tokens(None) == ""

    def test_small_number(self):
        assert format_tokens(500) == "500"

    def test_thousands(self):
        assert format_tokens(1500) == "1.5k"

    def test_millions(self):
        assert format_tokens(2_500_000) == "2.5M"

    @pytest.mark.parametrize("value", [999, 0, 1])
    def test_below_thousand(self, value):
        assert format_tokens(value) == str(value)
