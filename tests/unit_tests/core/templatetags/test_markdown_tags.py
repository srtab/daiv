from core.templatetags.markdown_tags import render_markdown


def test_empty_string_and_none_return_empty_string():
    assert render_markdown("") == ""
    assert render_markdown(None) == ""


def test_script_tag_and_onclick_attribute_are_removed():
    out = render_markdown('<script>alert("pwned")</script>hello')
    assert "<script>" not in out
    assert "alert" not in out
    assert "hello" in out

    out_click = render_markdown('<button onclick="alert(1)">Click me</button>')
    assert "onclick" not in out_click
    assert "Click me" in out_click


def test_javascript_link_is_neutralised():
    out = render_markdown("[click](javascript:alert(1))")
    assert "javascript:" not in out


def test_fenced_code_block_becomes_pre_code():
    out = render_markdown("```python\nprint(1)\n```")
    assert "<pre><code>" in out or ("<pre>" in out and "<code>" in out)
    assert "print(1)" in out


def test_markdown_table_becomes_table():
    md = "| A | B |\n|---|---|\n| 1 | 2 |"
    out = render_markdown(md)
    assert "<table>" in out
    assert "<th>" in out
    assert "<td>" in out


def test_single_newline_becomes_br():
    out = render_markdown("line 1\nline 2")
    assert "<br" in out
    assert "line 1" in out
    assert "line 2" in out


def test_consecutive_calls_do_not_leak_state():
    out1 = render_markdown("# Heading 1\n\nParagraph")
    assert "<h1>Heading 1</h1>" in out1

    out2 = render_markdown("Just text")
    assert "<h1>" not in out2
    assert "Just text" in out2
