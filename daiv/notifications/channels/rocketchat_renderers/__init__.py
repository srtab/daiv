"""Per-event Rocket Chat renderers. Each submodule self-registers via ``@register_renderer``."""

from notifications.channels.rocketchat_renderers import (  # noqa: F401
    job_batch_finished,
    job_finished,
    schedule_finished,
)
