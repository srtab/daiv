import pytest

from core.templatetags.icon_tags import icon


def test_renders_span_with_static_url():
    out = icon("trash", "h-4 w-4")
    assert 'class="inline-block h-4 w-4"' in out
    assert "url('/static/core/img/icons/trash.svg')" in out
    assert "background-color: currentColor" in out


def test_escapes_css_class():
    out = icon("trash", '"><script>alert(1)</script>')
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&quot;&gt;" in out


def test_empty_css_class_is_fine():
    out = icon("trash")
    assert 'class="inline-block "' in out


@pytest.mark.parametrize("bad", ["", "../etc/passwd", "a'b", "a b", "foo;bar", "a/b"])
def test_rejects_unsafe_names(bad):
    with pytest.raises(ValueError, match="filename slug"):
        icon(bad)
