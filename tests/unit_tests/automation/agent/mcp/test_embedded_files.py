import base64
import json

import pytest

from automation.agent.mcp.embedded_files import extract_embedded_files

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(200))
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(100))
PDF = b"%PDF-1.7\n" + bytes(range(150))
STEM = "/workspace/large_tool_results/call_1"


def test_base64_file_under_any_json_key_is_replaced_by_its_path():
    text = json.dumps({"name": "shot.png", "blob": base64.b64encode(PNG).decode()})

    new_text, files = extract_embedded_files(text, stem=STEM)

    assert files == [(f"{STEM}-1.png", PNG)]
    payload = json.loads(new_text)
    assert f"{STEM}-1.png" in payload["blob"] and "read_file" in payload["blob"]
    assert payload["name"] == "shot.png"


def test_json_escaped_line_breaks_and_slashes_are_decoded():
    """MIME base64 wraps lines, and some JSON encoders (PHP's) escape every slash."""
    text = json.dumps({"data": base64.encodebytes(PNG).decode()}).replace("/", "\\/")

    new_text, files = extract_embedded_files(text, stem=STEM)

    assert files == [(f"{STEM}-1.png", PNG)]
    assert f"{STEM}-1.png" in json.loads(new_text)["data"]


def test_data_uri_in_plain_text_is_replaced_whole():
    text = f"Before ![shot](data:image/jpeg;base64,{base64.b64encode(JPEG).decode()}) after"

    new_text, files = extract_embedded_files(text, stem=STEM)

    assert files == [(f"{STEM}-1.jpg", JPEG)]
    assert new_text.startswith("Before ![shot](") and new_text.endswith(") after")
    assert "data:image" not in new_text


def test_each_file_gets_its_own_numbered_path():
    text = json.dumps([base64.b64encode(PDF).decode(), "text", base64.b64encode(PNG).decode()])

    _, files = extract_embedded_files(text, stem=STEM)

    assert files == [(f"{STEM}-1.pdf", PDF), (f"{STEM}-2.png", PNG)]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("A plain sentence with no payload.", id="plain-text"),
        pytest.param(json.dumps({"token": base64.b64encode(b"x" * 300).decode()}), id="base64-of-text"),
        pytest.param(json.dumps({"zip": base64.b64encode(b"PK\x03\x04" + bytes(300)).decode()}), id="not-viewable"),
        pytest.param(json.dumps({"short": base64.b64encode(PNG[:40]).decode()}), id="too-short"),
        pytest.param(json.dumps({"cut": base64.b64encode(PNG).decode()[:-3]}), id="truncated"),
    ],
)
def test_text_without_a_viewable_file_is_left_alone(text):
    assert extract_embedded_files(text, stem=STEM) == (text, [])
