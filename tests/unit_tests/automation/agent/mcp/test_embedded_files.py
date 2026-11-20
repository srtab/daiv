import base64
import json
import re
from unittest.mock import AsyncMock

import pytest
from deepagents.backends.protocol import FileUploadResponse
from mcp.server.mcpserver import MCPServer

from automation.agent.mcp.client import list_tools
from automation.agent.mcp.embedded_files import extract_embedded_files, save_embedded_files
from automation.agent.middlewares.file_system import large_tool_results_prefix
from automation.agent.workspace.backends import DAIVFilesystemBackend
from tests.unit_tests.automation.agent.mcp.helpers import client_for, serve

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(200))
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(100))
PDF = b"%PDF-1.7\n" + bytes(range(150))
DIRECTORY = "/workspace/large_tool_results"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class TestExtractEmbeddedFiles:
    def test_base64_file_under_any_json_key_is_replaced_by_its_path(self):
        text = json.dumps({"name": "shot.png", "blob": _b64(PNG)})

        new_text, files = extract_embedded_files(text, directory=DIRECTORY)

        ((path, data),) = files.items()
        assert data == PNG
        assert path.startswith(f"{DIRECTORY}/") and path.endswith(".png")
        payload = json.loads(new_text)
        assert path in payload["blob"] and "read_file" in payload["blob"]
        assert payload["name"] == "shot.png"

    def test_json_escaped_line_breaks_and_slashes_are_decoded(self):
        """MIME base64 wraps lines, and some JSON encoders (PHP's) escape every slash."""
        text = json.dumps({"data": base64.encodebytes(PNG).decode()}).replace("/", "\\/")

        new_text, files = extract_embedded_files(text, directory=DIRECTORY)

        assert list(files.values()) == [PNG]
        assert next(iter(files)) in json.loads(new_text)["data"]

    def test_data_uri_in_plain_text_is_replaced_whole(self):
        text = f"Before ![shot](data:image/jpeg;base64,{_b64(JPEG)}) after"

        new_text, files = extract_embedded_files(text, directory=DIRECTORY)

        ((path, data),) = files.items()
        assert data == JPEG and path.endswith(".jpg")
        assert new_text.startswith("Before ![shot](") and new_text.endswith(") after")
        assert "data:image" not in new_text

    def test_each_distinct_file_is_saved_once(self):
        text = json.dumps([_b64(PDF), "text", _b64(PNG), _b64(PDF)])

        _, files = extract_embedded_files(text, directory=DIRECTORY)

        assert sorted(files.values()) == sorted([PDF, PNG])

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("A plain sentence with no payload.", id="plain-text"),
            pytest.param(json.dumps({"token": _b64(b"x" * 300)}), id="base64-of-text"),
            pytest.param(json.dumps({"zip": _b64(b"PK\x03\x04" + bytes(300))}), id="not-viewable"),
            pytest.param(json.dumps({"short": _b64(PNG[:40])}), id="too-short"),
            pytest.param(json.dumps({"cut": _b64(PNG)[:-3]}), id="truncated"),
        ],
    )
    def test_text_without_a_viewable_file_is_left_alone(self, text):
        assert extract_embedded_files(text, directory=DIRECTORY) == (text, {})


def _server_returning(payload: str) -> MCPServer:
    server = MCPServer(name="files-test")

    @server.tool()
    def fetch(item_id: int) -> str:
        """Fetch an item."""
        return payload

    return server


class TestSaveEmbeddedFiles:
    @pytest.fixture
    def backend(self, tmp_path):
        return DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)

    async def _call(self, backend, payload: str):
        async with serve(_server_returning(payload)) as gate:
            (tool,) = await list_tools(client_for(gate))
            save_embedded_files(tool, backend)
            return await tool.ainvoke({"type": "tool_call", "name": tool.name, "args": {"item_id": 1}, "id": "call_1"})

    async def test_file_is_saved_where_read_file_can_open_it(self, backend):
        message = await self._call(backend, json.dumps({"body": base64.encodebytes(PNG).decode()}))

        (path,) = re.findall(rf"{large_tool_results_prefix(backend)}/\w+\.png", message.text)
        assert (await backend.adownload_files([path]))[0].content == PNG

    async def test_large_file_no_longer_fills_the_result(self, backend):
        payload = json.dumps({"body": base64.encodebytes(PNG + bytes(range(256)) * 400).decode()})
        assert len(payload) > 80_000

        message = await self._call(backend, payload)

        assert len(message.text) < 1_000

    async def test_failed_upload_keeps_the_result_as_it_was(self, backend, monkeypatch):
        failed = AsyncMock(return_value=[FileUploadResponse(path="x", error="permission_denied")])
        monkeypatch.setattr(backend, "aupload_files", failed)
        payload = json.dumps({"body": _b64(PNG)})

        message = await self._call(backend, payload)

        assert message.text == payload
