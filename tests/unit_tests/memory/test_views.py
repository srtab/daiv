from __future__ import annotations

from unittest.mock import MagicMock

from django.urls import reverse

import pytest
from memory.models import MemoryObservation, ObservationCategory, ObservationStatus, RepositoryMemory


def _ss(memory_enabled=True):
    """Stub of the site-settings singleton for the memory views."""
    ss = MagicMock()
    ss.memory_enabled = memory_enabled
    return ss


@pytest.mark.django_db
def test_list_redirects_anonymous(client):
    resp = client.get(reverse("memory:list"))
    assert resp.status_code == 302
    assert "/accounts/login" in resp.url or "/login" in resp.url


@pytest.mark.django_db
def test_list_allows_member(client, member_user):
    client.force_login(member_user)
    resp = client.get(reverse("memory:list"))
    assert resp.status_code == 200


@pytest.mark.django_db
def test_list_unions_repos_from_both_tables(client, member_user):
    # Repo A: only observations, never consolidated.
    MemoryObservation.objects.create(repo_id="group/only-obs", category=ObservationCategory.PITFALL, content="x" * 20)
    MemoryObservation.objects.create(
        repo_id="group/only-obs",
        category=ObservationCategory.WORKFLOW,
        content="y" * 20,
        status=ObservationStatus.CONSOLIDATED,
    )
    # Repo B: only a consolidated document, no observation rows.
    RepositoryMemory.objects.create(repo_id="group/only-doc", content="# Memory\nsome content")

    client.force_login(member_user)
    resp = client.get(reverse("memory:list"))
    repos = {r["repo_id"]: r for r in resp.context["repos"]}

    assert set(repos) == {"group/only-doc", "group/only-obs"}
    assert list(repos) == sorted(repos)  # ordered by repo_id
    assert repos["group/only-obs"]["total"] == 2
    assert repos["group/only-obs"]["pending"] == 1
    assert repos["group/only-obs"]["consolidated"] == 1
    assert repos["group/only-obs"]["has_document"] is False
    assert repos["group/only-doc"]["total"] == 0
    assert repos["group/only-doc"]["has_document"] is True


@pytest.mark.django_db
def test_list_empty_state(client, member_user):
    client.force_login(member_user)
    resp = client.get(reverse("memory:list"))
    assert resp.status_code == 200
    assert resp.context["repos"] == []
    assert b"No repository memory yet" in resp.content
