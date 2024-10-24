import asyncio
import base64
import logging
import mimetypes
from collections.abc import Iterable
from urllib.parse import urlparse, urlunparse

import httpx

logger = logging.getLogger("daiv.core")


# https://platform.openai.com/docs/guides/vision/what-type-of-files-can-i-upload

SUPPORTED_MIMETYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def is_valid_url(url: str) -> bool:
    """
    Validate if the given string is a proper URL.
    """
    result = urlparse(url)
    return all([result.scheme, result.netloc])


def build_uri(uri: str, path: str):
    """
    Build a URI by appending a path to the given URI.
    """
    uri_parts = list(urlparse(uri))
    if uri_parts[2].endswith("/") and path.startswith("/"):
        uri_parts[2] += path[1:]
    elif not uri_parts[2].endswith("/") and not path.startswith("/"):
        uri_parts[2] += f"/{path}"
    else:
        uri_parts[2] += path
    return urlunparse(uri_parts)


def extract_image_mimetype_openai(image_url: str) -> str | None:
    """
    Check if the image URL has a supported mimetype.
    """
    mimetype, _encoding = mimetypes.guess_type(image_url)
    return mimetype in SUPPORTED_MIMETYPES and mimetype or None


def _url_to_data_url(client: httpx.Client, url: str) -> str | None:
    """
    Synchronously convert an image URL to a data URL.
    Returns None if the URL is invalid or the request fails.
    """
    if not (content_type := extract_image_mimetype_openai(url)):
        return None
    try:
        response = client.get(url)
        response.raise_for_status()
    except Exception:
        logger.warning("Failed to fetch image from URL: %s", url)
        return None
    else:
        base64_image = base64.b64encode(response.content).decode("utf-8")
        return f"data:{content_type};base64,{base64_image}"


def url_to_data_url(url: str, headers: dict[str, str] | None = None) -> str | None:
    """
    Synchronously convert an image URL to a data URL.
    Returns None if the URL is invalid or the request fails.
    """
    with httpx.Client(timeout=10.0, headers=headers) as client:
        return _url_to_data_url(client, url)


def batch_url_to_data_url(urls: list[str], headers: dict[str, str] | None = None) -> dict[str, str]:
    """
    Convert multiple URLs to data URLs synchronously.
    Returns a dictionary of URL to data URL mappings.
    """
    result = {}
    # Using a single client for all requests
    with httpx.Client(timeout=10.0, headers=headers) as client:
        for url in urls:
            if data_url := _url_to_data_url(client, url):
                result[url] = data_url
    return result


async def _async_url_to_data_url(client: httpx.AsyncClient, url: str) -> str | None:
    """
    Asynchronously convert an image URL to a data URL.
    Returns None if the URL is invalid or the request fails.
    """
    if not (content_type := extract_image_mimetype_openai(url)):
        return None
    try:
        response = await client.get(url)
        response.raise_for_status()
    except Exception:
        return None
    else:
        base64_image = base64.b64encode(response.content).decode("utf-8")
        return f"data:{content_type};base64,{base64_image}"


async def async_url_to_data_url(url: str) -> str | None:
    """
    Asynchronously convert an image URL to a data URL.
    Returns None if the URL is invalid or the request fails.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await _async_url_to_data_url(client, url)


async def batch_async_url_to_data_url(urls: Iterable[str], headers: dict[str, str]) -> dict[str, str]:
    """
    Convert multiple URLs to data URLs asynchronously.
    Returns a dictionary of URL to data URL mappings.
    """
    result = {}
    # Using a single client for all requests
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        tasks = []

        for url in urls:
            tasks.append(_async_url_to_data_url(client, url))

        responses = await asyncio.gather(*tasks, return_exceptions=True)

        for url, response in zip(urls, responses, strict=False):
            if isinstance(response, Exception):
                continue
            elif response is not None:
                result[url] = response

    return result
