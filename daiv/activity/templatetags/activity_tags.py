from decimal import Decimal

from django import template

from activity.models import TriggerType

register = template.Library()

_CENT = Decimal("0.01")
_TITLE_MAX_LEN = 100


@register.simple_tag
def activity_title(activity) -> str:
    """Derive a human-meaningful title for an Activity."""
    prompt = (activity.prompt or "").strip()
    if prompt:
        first_line = next((line for line in prompt.splitlines() if line.strip()), "").strip()
        if len(first_line) > _TITLE_MAX_LEN:
            return first_line[:_TITLE_MAX_LEN] + "…"
        return first_line

    if activity.trigger_type == TriggerType.ISSUE_WEBHOOK:
        return f"Issue #{activity.issue_iid}" if activity.issue_iid else "Issue"
    if activity.trigger_type == TriggerType.MR_WEBHOOK:
        return f"MR/PR !{activity.merge_request_iid}" if activity.merge_request_iid else "MR/PR"
    return f"{activity.get_trigger_type_display()} on {activity.repo_id}"


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
