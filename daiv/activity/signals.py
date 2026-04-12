from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger("daiv.activity")


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def backfill_activity_user(sender: type, instance: Any, created: bool, **kwargs: Any) -> None:
    """Link orphaned activities to a newly created user by matching external_username.

    Only runs on user creation, not updates — renaming a user will not re-trigger backfill.
    Errors are caught so that a problem in activity backfill never breaks user creation.
    """
    if not created:
        return

    from activity.models import Activity

    try:
        updated = Activity.objects.filter(user__isnull=True, external_username=instance.username).update(user=instance)
    except Exception:
        logger.exception("Failed to backfill activities for new user %s (pk=%s)", instance.username, instance.pk)
        return

    if updated:
        logger.info("Backfilled %d activities for new user %s (pk=%s)", updated, instance.username, instance.pk)
