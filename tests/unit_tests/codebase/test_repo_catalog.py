from datetime import timedelta

from django.utils import timezone

import pytest

from codebase.models import RepositoryCatalog, language_entry, matched_languages, matches_language


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


def _row(languages, **kwargs):
    return RepositoryCatalog(provider="gitlab", slug="a/b", languages=languages, **kwargs)


class TestRepositoryCatalogLanguages:
    def test_matches_language_at_threshold_inclusive(self):
        assert matches_language({"Python": 10.0}, "Python")
        assert not matches_language({"Python": 9.9}, "Python")

    def test_language_entry_is_case_insensitive_and_keeps_the_stored_spelling(self):
        assert language_entry({"Python": 41.2}, "python") == ("Python", 41.2)
        assert language_entry({"Python": 41.2}, "Go") is None

    def test_matched_languages_lists_stored_spellings_at_or_above_threshold(self):
        assert matched_languages({"TypeScript": 85.0, "Python": 10.0, "CSS": 5.0}) == ["TypeScript", "Python"]

    def test_primary_language_is_largest_share(self):
        assert _row({"TypeScript": 52.0, "Python": 41.0}).primary_language == "TypeScript"

    def test_language_label_uses_stored_spelling(self):
        row = _row({"TypeScript": 52.0, "Python": 41.4})

        assert row.language_label("python") == "Python 41%"
        assert row.language_label() == "TypeScript"
        assert row.language_label("Go") == "TypeScript"

    def test_empty_languages_have_no_primary_or_label(self):
        row = _row({})

        assert row.primary_language == ""
        assert row.language_label("Python") == ""
        assert not matches_language(row.languages, "Python")

    def test_needs_languages_refresh(self):
        t1 = timezone.now()
        t2 = t1 + timedelta(hours=1)
        fetched = _row({}, languages_synced_at=t1, languages_activity_at=t1)

        assert _row({}).needs_languages_refresh(t1)
        assert not fetched.needs_languages_refresh(t1)
        assert fetched.needs_languages_refresh(t2)
        assert not _row({}, languages_synced_at=t1, languages_activity_at=None).needs_languages_refresh(None)
