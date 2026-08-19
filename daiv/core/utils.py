import asyncio
import hashlib
import hmac
import logging
import mimetypes
from functools import wraps
from inspect import Signature, signature
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

from django.contrib.sites.models import Site
from django.core.cache import cache

import httpx
from redis.exceptions import LockError, LockNotOwnedError

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger("daiv.core")


# https://platform.openai.com/docs/guides/vision/what-type-of-files-can-i-upload

SUPPORTED_MIMETYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

mimetypes.add_type("image/webp", ".webp")  # Add webp mimetype, not included by default


def is_git_auth_error_text(text: str) -> bool:
    """True when git command output indicates the remote rejected the credential (auth/permission).

    Single source of truth shared by the clone-retry self-heal (``codebase.clients.gitlab``) and the
    push-failure classifier (``automation.agent.git_manager``) so both agree on what a rejected
    credential looks like — git's wording varies by version and by smart- vs dumb-HTTP. ``"access
    denied"`` already subsumes GitLab's ``"HTTP Basic: Access denied"``.
    """
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "authentication failed",
            "access denied",
            "returned error: 403",
            "could not read username",
            "permission denied",
            "not authorized",
        )
    )


def is_git_ref_not_found_text(text: str) -> bool:
    """True when git clone output indicates the requested branch/ref does not exist on the remote.

    Distinct from an auth or network failure: retrying is pointless — a deleted branch will not
    reappear — so callers fall back or skip instead of surfacing an opaque GitCommandError. Git's
    wording for ``git clone --branch <x>`` against a missing ref is ``Remote branch <x> not found
    in upstream origin``.
    """
    return "not found in upstream" in text.lower()


def build_absolute_url(path: str) -> str:
    """Build an absolute https:// URL from a relative path using the current Site domain."""
    site = Site.objects.get_current()
    return f"https://{site.domain}{path}"


def prefixed_email_subject(subject: str) -> str:
    """Prepend the current Site name as a bracketed prefix, e.g. ``[DAIV] Welcome``.

    Idempotent: safe to call on already-prefixed subjects. Returns the subject unchanged
    when the Site has no name or the Sites framework is misconfigured, so a missing Site
    row never breaks email delivery.
    """
    try:
        site = Site.objects.get_current()
    except Site.DoesNotExist:
        logger.warning("Site.objects.get_current() failed; sending email with unprefixed subject")
        return subject
    if not site.name:
        return subject
    prefix = f"[{site.name}] "
    if subject.startswith(prefix):
        return subject
    return f"{prefix}{subject}"


def is_valid_url(url: str) -> bool:
    """
    Validate if the given string is a proper URL.
    """
    result = urlparse(url)
    return all([result.scheme, result.netloc])


def is_htmx(request) -> bool:
    """True when the request was issued by HTMX (carries the ``HX-Request`` header).

    Shared by views that serve a results-only fragment to HTMX and the full page
    otherwise (e.g. ``SessionListView``, the ``sandbox_envs`` env views).
    """
    return request.headers.get("HX-Request") == "true"


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


def extract_valid_image_mimetype(image_content: bytes) -> str | None:
    """
    Check if the image content has a supported mimetype.
    """
    if image_content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(image_content) >= 6 and image_content[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if image_content.startswith(b"RIFF") and len(image_content) >= 12 and image_content[8:12] == b"WEBP":
        return "image/webp"
    return None


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
    return response.content


async def async_download_url(url: str, headers: dict[str, str] | None = None) -> bytes | None:
    """
    Asynchronously download an URL.
    Returns None if the URL is invalid or the request fails.
    """
    async with httpx.AsyncClient(timeout=10.0, headers=headers, follow_redirects=True) as client:
        return await _async_download_url(client, url)


async def batch_async_download_url(urls: Iterable[str], headers: dict[str, str] | None = None) -> dict[str, bytes]:
    """
    Download multiple URLs asynchronously.
    Returns a dictionary of URL to content mappings.
    """
    result = {}

    # Using a single client for all requests
    async with httpx.AsyncClient(timeout=10.0, headers=headers, follow_redirects=True) as client:
        responses = await asyncio.gather(*[_async_download_url(client, url) for url in urls], return_exceptions=True)

        for url, response in zip(urls, responses, strict=False):
            if isinstance(response, Exception):
                continue
            elif response is not None:
                result[url] = response

    return result


# Locks must expire: a holder killed mid-run strands the key in Redis forever, and every later
# run is then skipped as "already processing" while still reporting success.
DEFAULT_LOCK_TIMEOUT = 3600


def _format_lock_key(func, sig: Signature | None, key: str, args: tuple, kwargs: dict) -> str:
    """Render ``key`` against the call, so ``{repo_id}`` resolves however the caller passed it.

    Binding to the signature is what makes a named field work for a positional call — tasks are
    enqueued both ways. ``sig`` is ``None`` for an empty key, which formats to no suffix.
    """
    if sig is None:
        return f"{func.__name__}:"
    bound = sig.bind_partial(*args, **kwargs)
    bound.apply_defaults()
    return f"{func.__name__}:{key.format(*args, **bound.arguments)}"


def locked_task(key: str = "", blocking: bool = False, timeout: float = DEFAULT_LOCK_TIMEOUT):
    """
    A decorator that ensures a task is executed with a distributed lock to prevent concurrent execution.

    Args:
        key (str): A format string that will be used to generate the lock key. The format string can reference
                  positional and keyword arguments passed to the decorated function. Default is empty string.
        blocking (bool): If True, wait for the lock to be released. If False, raise LockError if lock is held.
                        Default is False.
        timeout (float): Seconds after which the lock expires on its own; must be positive. Size it above
                        the task's worst-case runtime, since expiring mid-run allows a concurrent run.

    Example:
        @task
        @locked_task(key="{repo_id}:{issue_iid}")  # Lock key will be: "task_name:repo123:issue456"
        def process_issue(repo_id: str, issue_iid: int):
            pass

    The lock is implemented using Django's cache backend, making it work in a distributed environment.
    If blocking=False and the lock is held, the task will be skipped with a warning message.
    """
    if timeout is None or timeout <= 0:
        raise ValueError(f"locked_task timeout must be a positive number of seconds, got {timeout!r}")

    def decorator(func):
        sig = signature(func) if key else None

        if asyncio.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                lock_key = _format_lock_key(func, sig, key, args, kwargs)
                try:
                    with cache.lock(lock_key, timeout=timeout, blocking=blocking):
                        return await func(*args, **kwargs)
                # Raised on release, and must precede its LockError superclass to not read as a skip.
                except LockNotOwnedError:
                    logger.error("Task outran its %ss lock, concurrent runs were possible: %s", timeout, lock_key)
                    return
                except LockError:
                    logger.warning("Ignored task, already processing: %s", lock_key)
                    return

            return async_wrapper
        else:

            @wraps(func)
            def wrapper(*args, **kwargs):
                lock_key = _format_lock_key(func, sig, key, args, kwargs)
                try:
                    with cache.lock(lock_key, timeout=timeout, blocking=blocking):
                        return func(*args, **kwargs)
                # Raised on release, and must precede its LockError superclass to not read as a skip.
                except LockNotOwnedError:
                    logger.error("Task outran its %ss lock, concurrent runs were possible: %s", timeout, lock_key)
                    return
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


def compute_keyed_uuid(input_string: str) -> str:
    """Deterministic, server-keyed identifier: HMAC-SHA256 of ``input_string`` keyed by
    a server secret (see :func:`core.encryption.get_thread_id_signing_key`).

    Returns 64 hex chars. Unlike :func:`generate_uuid` (unsalted MD5), this is **not**
    computable from public inputs alone — a caller who knows the ``(repo, scope, iid)``
    triple cannot pre-derive the webhook thread id to squat another repo's conversation.
    Use this for security-sensitive deterministic ids (webhook conversation thread ids);
    keep :func:`generate_uuid` for non-security cache keys.
    """
    from core.encryption import get_thread_id_signing_key

    key = get_thread_id_signing_key()
    return hmac.new(key, str(input_string).encode("utf-8"), hashlib.sha256).hexdigest()
