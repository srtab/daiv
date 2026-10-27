import logging

from django.core.cache import cache
from django.tasks import task

from codebase.clients import RepoClient

logger = logging.getLogger("daiv.dashboard")

BOT_USERNAME_CACHE_KEY = "dashboard:bot-username"
BOT_USERNAME_TIMEOUT = 60 * 60 * 24
BOT_USERNAME_RETRY_TIMEOUT = 60 * 5


@task(dedup=True, dedup_successful=False)
def resolve_bot_username_task() -> None:
    """Cache the account DAIV answers to on the git platform, for Home's Ways to use DAIV."""
    try:
        username = RepoClient.create_instance().current_user.username
    except Exception:
        logger.warning("Couldn't resolve DAIV's git platform username", exc_info=True)
        return
    cache.set(BOT_USERNAME_CACHE_KEY, username, BOT_USERNAME_TIMEOUT)
