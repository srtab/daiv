import base64
import logging
from unittest.mock import AsyncMock, Mock, patch

from automation.agents.middleware import InjectImagesMiddleware
from automation.agents.schemas import Image
from codebase.base import ClientType


class TestInjectImagesMiddleware:
    @patch("automation.agents.middleware.is_valid_url")
    async def test_from_images_valid_url(self, mock_is_valid_url):
        mock_is_valid_url.return_value = True
        images = [Image(url="http://example.com/image.png", filename="image.png")]

        result = await InjectImagesMiddleware()._images_to_content_blocks("repo_id", images)

        assert len(result) == 1
        assert "type" in result[0] and result[0]["type"] == "image"
        assert "url" in result[0] and result[0]["url"] == "http://example.com/image.png"
        assert "mime_type" not in result[0]

    @patch("automation.agents.middleware.is_valid_url")
    @patch("automation.agents.middleware.extract_valid_image_mimetype", new=Mock(return_value="image/png"))
    async def test_from_images_needs_build_uri(self, mock_is_valid_url):
        mock_is_valid_url.return_value = False
        result = await InjectImagesMiddleware()._images_to_content_blocks(
            "repo_id", [Image(url="uploads/image.png", filename="image.png")]
        )

        assert len(result) == 1
        assert "type" in result[0] and result[0]["type"] == "image"
        assert "base64" in result[0] and result[0]["base64"] == base64.b64encode(b"image content").decode()
        assert "mime_type" in result[0] and result[0]["mime_type"] == "image/png"
        assert "url" not in result[0]

    @patch("automation.agents.middleware.is_valid_url")
    async def test_from_images_invalid_gitlab_url(self, mock_is_valid_url):
        mock_is_valid_url.return_value = False
        images = [Image(url="invalid_url", filename="")]

        assert len(await InjectImagesMiddleware()._images_to_content_blocks("repo_id", images)) == 0

    @patch("automation.agents.middleware.extract_valid_image_mimetype", new=Mock(return_value="image/png"))
    async def test_from_images_github_user_attachments(self, mock_repo_client):
        """Test that GitHub user-attachments URLs are downloaded with authentication."""
        mock_repo_client.client_slug = ClientType.GITHUB
        mock_repo_client.get_project_uploaded_file = AsyncMock(return_value=b"github image content")

        images = [
            Image(
                url="https://github.com/user-attachments/assets/5005705d-f605-4dd7-b56c-eb513201b40e",
                filename="5005705d-f605-4dd7-b56c-eb513201b40e",
            )
        ]

        result = await InjectImagesMiddleware()._images_to_content_blocks("repo_id", images)

        assert len(result) == 1
        assert result[0]["type"] == "image"
        assert result[0]["base64"] == base64.b64encode(b"github image content").decode()
        assert result[0]["mime_type"] == "image/png"
        assert "url" not in result[0]

        # Verify the download was called with the correct URL
        mock_repo_client.get_project_uploaded_file.assert_called_once()
        call_args = mock_repo_client.get_project_uploaded_file.call_args
        assert call_args[0][1] == "https://github.com/user-attachments/assets/5005705d-f605-4dd7-b56c-eb513201b40e"

    async def test_from_images_github_user_attachments_download_fails(self, mock_repo_client):
        """Test that failed GitHub downloads don't add to result."""
        mock_repo_client.client_slug = ClientType.GITHUB
        mock_repo_client.get_project_uploaded_file = AsyncMock(return_value=None)

        images = [
            Image(
                url="https://github.com/user-attachments/assets/5005705d-f605-4dd7-b56c-eb513201b40e",
                filename="5005705d-f605-4dd7-b56c-eb513201b40e",
            )
        ]

        result = await InjectImagesMiddleware()._images_to_content_blocks("repo_id", images)

        assert len(result) == 0

    async def test_from_images_github_external_url_not_user_attachments(self, mock_repo_client):
        """Test that non-user-attachments GitHub URLs are treated as regular URLs."""
        mock_repo_client.client_slug = ClientType.GITHUB

        with patch("automation.agents.middleware.is_valid_url", return_value=True):
            images = [Image(url="https://github.com/user/repo/raw/main/image.png", filename="image.png")]

            result = await InjectImagesMiddleware()._images_to_content_blocks("repo_id", images)

            assert len(result) == 1
            assert "url" in result[0] and result[0]["url"] == "https://github.com/user/repo/raw/main/image.png"


class TestToolCallLoggingMiddleware:
    async def test_logs_tool_call_start_and_finish(self, caplog):
        from langchain_core.messages import ToolMessage
        from langgraph.prebuilt.tool_node import ToolCallRequest

        from automation.agents.middleware import ToolCallLoggingMiddleware

        caplog.set_level(logging.INFO, logger="daiv.tools")

        request = ToolCallRequest(
            tool_call={"name": "demo_tool", "args": {"x": 1}, "id": "call_1"},
            tool=None,
            state={"messages": []},
            runtime=Mock(),
        )

        async def handler(req: ToolCallRequest):
            return ToolMessage(content="ok", tool_call_id=req.tool_call["id"], name=req.tool_call["name"])

        result = await ToolCallLoggingMiddleware().awrap_tool_call(request, handler)
        assert isinstance(result, ToolMessage)
        assert result.content == "ok"

        messages = [r.getMessage() for r in caplog.records if r.name == "daiv.tools"]
        assert any("[demo_tool] Tool call started" in m for m in messages)
        assert any("[demo_tool] Tool call finished" in m for m in messages)

    async def test_logs_tool_call_exception_and_reraises(self, caplog):
        from langgraph.prebuilt.tool_node import ToolCallRequest

        from automation.agents.middleware import ToolCallLoggingMiddleware

        caplog.set_level(logging.INFO, logger="daiv.tools")

        request = ToolCallRequest(
            tool_call={"name": "demo_tool", "args": {"x": 1}, "id": "call_1"},
            tool=None,
            state={"messages": []},
            runtime=Mock(),
        )

        async def handler(_req: ToolCallRequest):
            raise ValueError("boom")

        import pytest

        with pytest.raises(ValueError, match="boom"):
            await ToolCallLoggingMiddleware().awrap_tool_call(request, handler)

        messages = [r.getMessage() for r in caplog.records if r.name == "daiv.tools"]
        assert any("[demo_tool] Tool call failed" in m for m in messages)
