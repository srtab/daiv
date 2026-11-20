"""Validation shared by configured search endpoints and their runtime clients."""

from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.utils.translation import gettext_lazy as _


def validate_searxng_url(value: str) -> None:
    """Accept complete HTTP(S) endpoints, including internal Docker service names."""
    message = _("Use a complete http or https SearXNG URL without credentials, query parameters or a fragment.")
    try:
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value) or any(
            char in value for char in "\\?#"
        ):
            raise ValueError
        parsed = urlsplit(value)
        hostname, port = parsed.hostname, parsed.port
        if not hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError
        # Validate Docker DNS names through a surrogate accepted by Django, preserving
        # the endpoint itself: service names may have underscores or no domain suffix.
        validation_hostname = hostname.replace("_", "a")
        if "." not in validation_hostname and ":" not in validation_hostname and validation_hostname != "localhost":
            validation_hostname += ".internal"
        validation_url = value
        if validation_hostname != hostname:
            netloc = validation_hostname
            if port is not None:
                netloc += f":{port}"
            validation_url = parsed._replace(netloc=netloc).geturl()
        URLValidator(schemes=["http", "https"])(validation_url)
    except ValueError, ValidationError:
        raise ValidationError(message, code="invalid") from None
