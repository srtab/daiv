"""Template filters for the env picker partial."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import template

from sandbox_envs.services import humanise_env_summary

if TYPE_CHECKING:
    from sandbox_envs.models import SandboxEnvironment

register = template.Library()


@register.filter(name="humanise_env_summary_for_template")
def humanise_env_summary_filter(env: SandboxEnvironment) -> str:
    return humanise_env_summary(env)
