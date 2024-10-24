import base64
import pytest
from core.utils import (
    is_valid_url,
    build_uri,
    extract_image_mimetype_openai,
    url_to_data_url,
    batch_url_to_data_url,
    async_url_to_data_url,
    batch_async_url_to_data_url,
)


class IsValidUrlTest:
    def test_valid_url_returns_true(self):
        assert is_valid_url("https://example.com/image.jpg") is True
        assert is_valid_url("http://test.org/path/to/resource") is True

    def test_invalid_url_returns_false(self):
        assert is_valid_url("not-a-url") is False
        assert is_valid_url("") is False
        assert is_valid_url("file:///local/path") is False


class BuildUriTest:
    def test_append_path_to_base_uri(self):
        assert build_uri("https://api.example.com", "v1/endpoint") == "https://api.example.com/v1/endpoint"

    def test_handle_double_slash(self):
        assert build_uri("https://api.example.com/", "/v1/endpoint") == "https://api.example.com/v1/endpoint"

    def test_handle_missing_slash(self):
        assert build_uri("https://api.example.com", "v1/endpoint") == "https://api.example.com/v1/endpoint"


class ExtractImageMimetypeOpenaiTest:
    def test_supported_image_formats(self):
        assert extract_image_mimetype_openai("image.jpg") == "image/jpeg"
        assert extract_image_mimetype_openai("image.jpeg") == "image/jpeg"
        assert extract_image_mimetype_openai("image.png") == "image/png"
        assert extract_image_mimetype_openai("image.gif") == "image/gif"
        assert extract_image_mimetype_openai("image.webp") == "image/webp"

    def test_unsupported_image_formats(self):
        assert extract_image_mimetype_openai("image.bmp") is None
        assert extract_image_mimetype_openai("image.tiff") is None
        assert extract_image_mimetype_openai("not-an-image.txt") is None


class UrlToDataUrlTest:
    def test_successful_conversion(self, mocker):
        mock_response = mocker.Mock()
        mock_response.content = b"fake-image-data"
        mock_response.raise_for_status.return_value = None
        
        mock_client = mocker.patch("httpx.Client")
        mock_client.return_value.__enter__.return_value.get.return_value = mock_response

        expected_data_url = f"data:image/jpeg;base64,{base64.b64encode(b'fake-image-data').decode('utf-8')}"
        result = url_to_data_url("https://example.com/image.jpg")
        
        assert result == expected_data_url

    def test_failed_request(self, mocker):
        mock_client = mocker.patch("httpx.Client")
        mock_client.return_value.__enter__.return_value.get.side_effect = Exception("Request failed")

        result = url_to_data_url("https://example.com/image.jpg")
        assert result is None


class BatchUrlToDataUrlTest:
    def test_successful_batch_conversion(self, mocker):
        mock_response = mocker.Mock()
        mock_response.content = b"fake-image-data"
        mock_response.raise_for_status.return_value = None
        
        mock_client = mocker.patch("httpx.Client")
        mock_client.return_value.__enter__.return_value.get.return_value = mock_response

        urls = ["https://example.com/1.jpg", "https://example.com/2.jpg"]
        expected_data_url = f"data:image/jpeg;base64,{base64.b64encode(b'fake-image-data').decode('utf-8')}"
        
        result = batch_url_to_data_url(urls)
        assert len(result) == 2
        assert all(url in result for url in urls)
        assert all(data_url == expected_data_url for data_url in result.values())

    def test_partial_failed_requests(self, mocker):
        mock_client = mocker.patch("httpx.Client")
        client = mock_client.return_value.__enter__.return_value
        
        # First request succeeds, second fails
        mock_response_success = mocker.Mock()
        mock_response_success.content = b"fake-image-data"
        mock_response_success.raise_for_status.return_value = None
        
        def get_side_effect(url):
            if "1.jpg" in url:
                return mock_response_success
            raise Exception("Request failed")
        
        client.get.side_effect = get_side_effect
        
        urls = ["https://example.com/1.jpg", "https://example.com/2.jpg"]
        result = batch_url_to_data_url(urls)
        
        assert len(result) == 1
        assert "https://example.com/1.jpg" in result


class TestAsyncUrlToDataUrl:
    @pytest.mark.asyncio
    async def test_successful_conversion(self, mocker):
        mock_response = mocker.Mock()
        mock_response.content = b"fake-image-data"
        mock_response.raise_for_status.return_value = None
        
        mock_client = mocker.patch("httpx.AsyncClient")
        mock_client.return_value.__aenter__.return_value.get.return_value = mock_response

        expected_data_url = f"data:image/jpeg;base64,{base64.b64encode(b'fake-image-data').decode('utf-8')}"
        result = await async_url_to_data_url("https://example.com/image.jpg")
        
        assert result == expected_data_url

    @pytest.mark.asyncio
    async def test_failed_request(self, mocker):
        mock_client = mocker.patch("httpx.AsyncClient")
        mock_client.return_value.__aenter__.return_value.get.side_effect = Exception("Request failed")

        result = await async_url_to_data_url("https://example.com/image.jpg")
        assert result is None


class BatchAsyncUrlToDataUrlTest:
    @pytest.mark.asyncio
    async def test_successful_batch_conversion(self, mocker):
        mock_response = mocker.Mock()
        mock_response.content = b"fake-image-data"
        mock_response.raise_for_status.return_value = None
        
        mock_client = mocker.patch("httpx.AsyncClient")
        mock_client.return_value.__aenter__.return_value.get.return_value = mock_response

        urls = ["https://example.com/1.jpg", "https://example.com/2.jpg"]
        expected_data_url = f"data:image/jpeg;base64,{base64.b64encode(b'fake-image-data').decode('utf-8')}"
        
        result = await batch_async_url_to_data_url(urls, headers={})
        assert len(result) == 2
        assert all(url in result for url in urls)
        assert all(data_url == expected_data_url for data_url in result.values())

    @pytest.mark.asyncio
    async def test_partial_failed_requests(self, mocker):
        mock_client = mocker.patch("httpx.AsyncClient")
        client = mock_client.return_value.__aenter__.return_value
        
        # First request succeeds, second fails
        mock_response_success = mocker.Mock()
        mock_response_success.content = b"fake-image-data"
        mock_response_success.raise_for_status.return_value = None
        
        def get_side_effect(url):
            if "1.jpg" in url:
                return mock_response_success
            raise Exception("Request failed")
        
        client.get.side_effect = get_side_effect
        
        urls = ["https://example.com/1.jpg", "https://example.com/2.jpg"]
        result = await batch_async_url_to_data_url(urls, headers={})
        
        assert len(result) == 1
        assert "https://example.com/1.jpg" in result
