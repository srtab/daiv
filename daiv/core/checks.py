from django.core import checks

from get_docker_secret import get_docker_secret

REMOVED_ENV_VARS = {
    "DAIV_AGENT_EXPLORE_MODEL_NAME": 'Set it as the "explore" entry of DAIV_AGENT_SUBAGENT_MODELS instead.',
    "DAIV_AGENT_EXPLORE_FALLBACK_MODEL_NAME": "The explore subagent now falls back to the agent's fallback models.",
}


@checks.register("core")
def check_removed_env_vars(app_configs, **kwargs):
    """Warn about a removed setting still set as an environment variable or Docker secret, which DAIV ignores."""
    return [
        checks.Warning(f"{name} is no longer read.", hint=hint, id="core.W001")
        for name, hint in REMOVED_ENV_VARS.items()
        if get_docker_secret(name, default=None) is not None
    ]
