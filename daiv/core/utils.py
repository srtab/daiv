import asyncio
import hashlib
import logging
import mimetypes
from collections.abc import Iterable
from functools import wraps
from urllib.parse import urlparse, urlunparse

from django.core.cache import cache

import httpx
from redis.exceptions import LockError

logger = logging.getLogger("daiv.core")


# https://platform.openai.com/docs/guides/vision/what-type-of-files-can-i-upload

SUPPORTED_MIMETYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

mimetypes.add_type("image/webp", ".webp")  # Add webp mimetype, not included by default


def is_valid_url(url: str) -> bool:
    """
    Validate if the given string is a proper URL.
    """
    result = urlparse(url)
    return all([result.scheme, result.netloc])


def build_uri(uri: str, path: str):
    """
    Build a URI by appending a path to the given URI.
    Ensures there is exactly one slash between the URI and path.
    """
    uri_parts = list(urlparse(uri))
    # Strip trailing slashes from the path component
    uri_parts[2] = uri_parts[2].rstrip("/")
    # Strip leading slashes from the new path
    clean_path = path.lstrip("/")
    # Add a single slash between URI and path
    uri_parts[2] = f"{uri_parts[2]}/{clean_path}"
    return urlunparse(uri_parts)


def extract_valid_image_mimetype(image_url: str) -> str | None:
    """
    Check if the image URL has a supported mimetype.
    """
    mimetype, _encoding = mimetypes.guess_type(image_url)
    return mimetype in SUPPORTED_MIMETYPES and mimetype or None


async def _async_download_url(client: httpx.AsyncClient, url: str) -> bytes | None:
    """
    Asynchronously download an URL.
    Returns None if the URL is invalid or the request fails.
    """
    try:
        response = await client.get(url)
        response.raise_for_status()
    except Exception:
        return None
    else:
        return response.content


async def async_download_url(url: str, headers: dict[str, str] | None = None) -> bytes | None:
    """
    Asynchronously download an URL.
    Returns None if the URL is invalid or the request fails.
    """
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        return await _async_download_url(client, url)


async def batch_async_download_url(urls: Iterable[str], headers: dict[str, str] | None = None) -> dict[str, bytes]:
    """
    Download multiple URLs asynchronously.
    Returns a dictionary of URL to content mappings.
    """
    result = {}

    # Using a single client for all requests
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        responses = await asyncio.gather(*[_async_download_url(client, url) for url in urls], return_exceptions=True)

        for url, response in zip(urls, responses, strict=False):
            if isinstance(response, Exception):
                continue
            elif response is not None:
                result[url] = response

    return result


def locked_task(key: str = "", blocking: bool = False):
    """
    A decorator that ensures a task is executed with a distributed lock to prevent concurrent execution.

    Args:
        key (str): A format string that will be used to generate the lock key. The format string can reference
                  positional and keyword arguments passed to the decorated function. Default is empty string.
        blocking (bool): If True, wait for the lock to be released. If False, raise LockError if lock is held.
                        Default is False.

    Example:
        @shared_task
        @locked_task(key="{repo_id}:{issue_iid}")  # Lock key will be: "task_name:repo123:issue456"
        def process_issue(repo_id: str, issue_iid: int):
            pass

    The lock is implemented using Django's cache backend, making it work in a distributed environment.
    If blocking=False and the lock is held, the task will be skipped with a warning message.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            lock_key = f"{func.__name__}:{key.format(*args, **kwargs)}"
            try:
                with cache.lock(lock_key, blocking=blocking):
                    func(*args, **kwargs)
            except LockError:
                logger.warning("Ignored task, already processing: %s", lock_key)
                return

        return wrapper

    return decorator


def generate_uuid(input_string: str) -> str:
    """
    Generate a deterministic UUID from a string.
    """
    input_bytes = str(input_string).encode("utf-8")
    return hashlib.md5(input_bytes).hexdigest()  # noqa: S324
