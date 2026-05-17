"""Template filters for the env picker partial."""

from django import template

from sandbox_envs.services import humanise_env_summary

register = template.Library()

register.filter("humanise_env_summary_for_template", humanise_env_summary)
