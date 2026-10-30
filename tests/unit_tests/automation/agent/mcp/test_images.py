import base64
import json

import pytest

from automation.agent.mcp.images import extract_inline_images

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(200))
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(100))
STEM = "/workspace/large_tool_results/call_rt"


def _rt_attachment(content_type: str, content: str | None) -> str:
    """The shape RT's ``get_attachment`` returns: MIME-wrapped base64 under ``Content``."""
    return json.dumps({
        "id": 14039426,
        "ContentType": content_type,
        "Filename": "Screenshot 2026-10-24 084622.png",
        "Headers": "Content-Transfer-Encoding: base64",
        "Content": content,
    })


def test_rt_attachment_image_is_replaced_by_its_file_path():
    text, files = extract_inline_images(_rt_attachment("image/png", base64.encodebytes(PNG).decode()), stem=STEM)

    assert files == [(f"{STEM}-image-1.png", PNG)]
    payload = json.loads(text)
    assert f"{STEM}-image-1.png" in payload["Content"]
    assert "read_file" in payload["Content"]
    assert payload["Filename"] == "Screenshot 2026-10-24 084622.png"


def test_each_image_nested_in_a_transaction_gets_its_own_file():
    transaction = json.dumps({
        "Attachments": [
            {"ContentType": "image/png", "Content": base64.b64encode(PNG).decode()},
            {"ContentType": "text/html", "Content": "<p>Bom dia</p>"},
            {"ContentType": "image/jpeg", "Content": base64.b64encode(JPEG).decode()},
        ]
    })

    text, files = extract_inline_images(transaction, stem=STEM)

    assert files == [(f"{STEM}-image-1.png", PNG), (f"{STEM}-image-2.jpg", JPEG)]
    assert json.loads(text)["Attachments"][1]["Content"] == "<p>Bom dia</p>"


def test_mcp_style_mime_type_and_data_fields_are_extracted():
    text, files = extract_inline_images(
        json.dumps({"mimeType": "image/png", "data": base64.b64encode(PNG).decode()}), stem=STEM
    )

    assert files == [(f"{STEM}-image-1.png", PNG)]
    assert base64.b64encode(PNG).decode() not in text


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Attachment image/png saved", id="not-json"),
        pytest.param(_rt_attachment("image/png", "https://support.example.com/a.png"), id="not-base64"),
        pytest.param(_rt_attachment("image/png", None), id="no-content"),
        pytest.param(_rt_attachment("application/pdf", base64.b64encode(PNG).decode()), id="not-an-image"),
    ],
)
def test_text_without_an_inline_image_is_left_alone(text):
    assert extract_inline_images(text, stem=STEM) == (text, [])
