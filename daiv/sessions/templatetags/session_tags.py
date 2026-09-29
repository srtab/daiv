from decimal import Decimal

from django import template
from django.contrib.humanize.templatetags.humanize import naturaltime
from django.utils import formats, timezone
from django.utils.translation import gettext

register = template.Library()

_CENT = Decimal("0.01")


@register.simple_tag
def session_title(session) -> str:
    """Derive a human-meaningful title for a Session."""
    if stored := (session.title or "").strip():
        return stored

    # Fall back to the thread_id prefix so there is always something to show.
    return session.thread_id[:8]


@register.filter
def duration(value):
    """Format a duration in seconds as a compact human-readable string."""
    if value is None:
        return ""
    total_seconds = int(value)
    if total_seconds < 0:
        return ""
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


@register.filter
def format_cost(value):
    """Format a Decimal cost as a compact USD string."""
    if value is None:
        return ""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    if d < _CENT:
        return f"${d:.4f}"
    return f"${d:.2f}"


@register.filter
def format_tokens(value):
    """Format token count with compact suffixes (1.2k, 45.3k, 1.2M)."""
    if value is None:
        return ""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


@register.filter
def approx_prompt_tokens(prompt) -> int:
    """Rough token count via the 4-chars-per-token heuristic; avoids a tokenizer dep for display-only hints."""
    if not prompt:
        return 0
    return len(str(prompt)) // 4


_STATUS_VARIANTS = {
    "SUCCESSFUL": "success",
    "FAILED": "failed",
    "RUNNING": "running",
    "QUEUED": "queued",
    "WAITING_INPUT": "waiting",
}


@register.filter
def status_variant(status) -> str:
    """Map RunStatus to the CSS/Alpine variant suffix used by status-badge / status-dot."""
    return _STATUS_VARIANTS.get(status, "pending")


_ORIGIN_ICONS = {
    "chat": "chat-bubble",
    "api_job": "command-line",
    "mcp_job": "cube",
    "schedule": "clock",
    "ui_job": "bolt",
    "issue_webhook": "exclamation-circle",
    "mr_webhook": "merge-request",
}


@register.filter
def origin_icon(origin) -> str:
    """Map a SessionOrigin value to an icon name; unknown → generic 'jobs'."""
    return _ORIGIN_ICONS.get(origin, "jobs")


def _local_days_ago(dt) -> int:
    return (timezone.localtime(timezone.now()).date() - timezone.localtime(dt).date()).days


@register.filter
def date_bucket(dt) -> str:
    """Group label for a datetime, relative to the local calendar day."""
    if dt is None:
        return gettext("Earlier")
    days = _local_days_ago(dt)
    if days <= 0:
        return gettext("Today")
    if days == 1:
        return gettext("Yesterday")
    if days <= 7:
        return gettext("Previous 7 days")
    if days <= 30:
        return gettext("Previous 30 days")
    return timezone.localtime(dt).strftime("%B %Y")


@register.filter
def short_timestamp(dt) -> str:
    """Relative time for today's datetimes, the local month and day for older ones."""
    if _local_days_ago(dt) <= 0:
        return naturaltime(dt)
    return formats.date_format(timezone.localtime(dt), "M j")


_ARTIFACT_ICONS = {"markdown": "document-text", "html": "code-bracket", "image": "photo", "text": "document"}


@register.filter
def artifact_icon(kind) -> str:
    """Map an ArtifactKind value to an icon name; anything else is a generic file."""
    return _ARTIFACT_ICONS.get(kind, "paper-clip")


@register.simple_tag
def session_cost(session) -> str:
    """Sum cost_usd across a session's runs (uses the prefetch cache), formatted."""
    total = sum((r.cost_usd for r in session.runs.all() if r.cost_usd is not None), Decimal("0"))
    return format_cost(total) if total > 0 else ""
