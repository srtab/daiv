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


@pytest.mark.django_db
def test_detail_404_for_unknown_repo(client, member_user):
    client.force_login(member_user)
    resp = client.get(reverse("memory:detail", args=["group/nope"]))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_detail_renders_never_consolidated_repo(client, member_user):
    MemoryObservation.objects.create(repo_id="group/proj", category=ObservationCategory.CODEBASE_FACT, content="z" * 20)
    client.force_login(member_user)
    resp = client.get(reverse("memory:detail", args=["group/proj"]))
    assert resp.status_code == 200
    assert resp.context["memory"] is None
    assert resp.context["total_observations"] == 1


@pytest.mark.django_db
def test_detail_renders_document_markdown(client, member_user):
    RepositoryMemory.objects.create(repo_id="group/proj", content="# Heading\n\nbody text")
    client.force_login(member_user)
    resp = client.get(reverse("memory:detail", args=["group/proj"]))
    assert resp.status_code == 200
    # render_markdown turns the markdown heading into an <h1>.
    assert b"<h1>Heading</h1>" in resp.content


@pytest.mark.django_db
def test_detail_filters_by_status_and_category(client, member_user):
    MemoryObservation.objects.create(
        repo_id="group/proj", category=ObservationCategory.PITFALL, content="p" * 20, status=ObservationStatus.PENDING
    )
    MemoryObservation.objects.create(
        repo_id="group/proj",
        category=ObservationCategory.WORKFLOW,
        content="w" * 20,
        status=ObservationStatus.CONSOLIDATED,
    )
    client.force_login(member_user)

    resp = client.get(reverse("memory:detail", args=["group/proj"]), {"status": "pending"})
    assert [o.status for o in resp.context["page_obj"]] == ["pending"]
    assert resp.context["current_status"] == "pending"

    resp = client.get(reverse("memory:detail", args=["group/proj"]), {"category": "workflow"})
    assert [o.category for o in resp.context["page_obj"]] == ["workflow"]

    # Invalid filter value is ignored (no crash, no filtering).
    resp = client.get(reverse("memory:detail", args=["group/proj"]), {"status": "bogus"})
    assert resp.context["current_status"] == ""
    assert len(resp.context["page_obj"]) == 2


@pytest.mark.django_db
def test_detail_paginates_observations(client, member_user):
    MemoryObservation.objects.bulk_create([
        MemoryObservation(
            repo_id="group/proj", category=ObservationCategory.CODEBASE_FACT, content=f"obs {i:03d} padding"
        )
        for i in range(51)
    ])
    client.force_login(member_user)
    resp = client.get(reverse("memory:detail", args=["group/proj"]))
    assert resp.context["is_paginated"] is True
    assert len(resp.context["page_obj"]) == 50
    resp2 = client.get(reverse("memory:detail", args=["group/proj"]), {"page": 2})
    assert len(resp2.context["page_obj"]) == 1


@pytest.mark.django_db
def test_detail_resolves_repo_id_with_multiple_slashes(client, member_user):
    RepositoryMemory.objects.create(repo_id="group/sub/proj", content="x")
    client.force_login(member_user)
    resp = client.get(reverse("memory:detail", args=["group/sub/proj"]))
    assert resp.status_code == 200
    assert resp.context["repo_id"] == "group/sub/proj"
