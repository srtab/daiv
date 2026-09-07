"""Per-event Telegram renderers. Each submodule self-registers via ``@register_renderer``."""

from notifications.channels.telegram_renderers import job_batch_finished, job_finished, schedule_finished  # noqa: F401
