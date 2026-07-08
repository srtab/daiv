from datetime import timedelta

from django.utils import timezone

import pytest

from codebase.models import RepositoryCatalog


def _cat(slug: str, synced_at):
    return RepositoryCatalog.objects.create(
        provider="gitlab",
        slug=slug,
        name=slug.split("/")[-1],
        default_branch="main",
        html_url=f"https://example/{slug}",
        topics=[],
        synced_at=synced_at,
    )


@pytest.mark.django_db
class TestRepositoryCatalogFreshness:
    def test_fresh_includes_recent_and_excludes_stale(self):
        now = timezone.now()
        _cat("a/recent", now)
        _cat("b/stale", now - timedelta(hours=48))

        fresh_slugs = set(RepositoryCatalog.objects.fresh().values_list("slug", flat=True))

        assert fresh_slugs == {"a/recent"}
