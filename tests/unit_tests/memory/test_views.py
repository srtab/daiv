from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

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


@pytest.mark.django_db
def test_detail_reports_document_stats_when_consolidated(client, member_user):
    # document_lines/document_bytes and the "Last consolidated" header only render once
    # last_consolidated_at is set; the multi-byte char pins bytes (not chars) as the unit.
    content = "# Heading\n\nünïcode body\nthird line"
    RepositoryMemory.objects.create(repo_id="group/proj", content=content, last_consolidated_at=timezone.now())
    client.force_login(member_user)
    resp = client.get(reverse("memory:detail", args=["group/proj"]))
    assert resp.status_code == 200
    assert resp.context["document_lines"] == len(content.splitlines())
    assert resp.context["document_bytes"] == len(content.encode("utf-8"))
    assert resp.context["document_bytes"] > len(content)
    assert b"Last consolidated" in resp.content
    assert b"Never consolidated" not in resp.content


@pytest.mark.django_db
def test_detail_shows_consolidate_button_for_admin(client, admin_user):
    RepositoryMemory.objects.create(repo_id="group/proj", content="x")
    client.force_login(admin_user)
    with patch("memory.views.site_settings", _ss(memory_enabled=True)):
        resp = client.get(reverse("memory:detail", args=["group/proj"]))
    assert resp.status_code == 200
    assert reverse("memory:consolidate", args=["group/proj"]).encode() in resp.content


@pytest.mark.django_db
def test_detail_hides_consolidate_button_for_member(client, member_user):
    RepositoryMemory.objects.create(repo_id="group/proj", content="x")
    client.force_login(member_user)
    with patch("memory.views.site_settings", _ss(memory_enabled=True)):
        resp = client.get(reverse("memory:detail", args=["group/proj"]))
    assert reverse("memory:consolidate", args=["group/proj"]).encode() not in resp.content


@pytest.mark.django_db
def test_detail_hides_consolidate_button_when_memory_disabled(client, admin_user):
    RepositoryMemory.objects.create(repo_id="group/proj", content="x")
    client.force_login(admin_user)
    with patch("memory.views.site_settings", _ss(memory_enabled=False)):
        resp = client.get(reverse("memory:detail", args=["group/proj"]))
    assert reverse("memory:consolidate", args=["group/proj"]).encode() not in resp.content


@pytest.mark.django_db
def test_consolidate_admin_enqueues_and_redirects(client, admin_user):
    MemoryObservation.objects.create(repo_id="group/proj", category=ObservationCategory.PITFALL, content="p" * 20)
    client.force_login(admin_user)
    with (
        patch("memory.views.site_settings", _ss(memory_enabled=True)),
        patch("memory.views.consolidate_memory_task") as task_mock,
    ):
        resp = client.post(reverse("memory:consolidate", args=["group/proj"]))
    task_mock.enqueue.assert_called_once_with("group/proj")
    assert resp.status_code == 302
    assert resp.url == reverse("memory:detail", args=["group/proj"])
    msgs = [str(m) for m in get_messages(resp.wsgi_request)]
    assert any("queued" in m.lower() for m in msgs)


@pytest.mark.django_db
def test_consolidate_no_op_when_no_pending_observations(client, admin_user):
    # A repo with a document but zero pending observations: the task would no-op, so the view
    # must neither enqueue nor claim success — it reports there is nothing to do instead.
    RepositoryMemory.objects.create(repo_id="group/proj", content="# Memory")
    MemoryObservation.objects.create(
        repo_id="group/proj",
        category=ObservationCategory.WORKFLOW,
        content="w" * 20,
        status=ObservationStatus.CONSOLIDATED,
    )
    client.force_login(admin_user)
    with (
        patch("memory.views.site_settings", _ss(memory_enabled=True)),
        patch("memory.views.consolidate_memory_task") as task_mock,
    ):
        resp = client.post(reverse("memory:consolidate", args=["group/proj"]))
    task_mock.enqueue.assert_not_called()
    assert resp.status_code == 302
    msgs = [str(m) for m in get_messages(resp.wsgi_request)]
    assert any("nothing to consolidate" in m.lower() for m in msgs)


@pytest.mark.django_db
def test_consolidate_denies_member(client, member_user):
    client.force_login(member_user)
    with patch("memory.views.consolidate_memory_task") as task_mock:
        resp = client.post(reverse("memory:consolidate", args=["group/proj"]))
    assert resp.status_code == 403
    task_mock.enqueue.assert_not_called()


@pytest.mark.django_db
def test_consolidate_rejects_get(client, admin_user):
    client.force_login(admin_user)
    resp = client.get(reverse("memory:consolidate", args=["group/proj"]))
    assert resp.status_code == 405


@pytest.mark.django_db
def test_consolidate_no_op_when_memory_disabled(client, admin_user):
    client.force_login(admin_user)
    with (
        patch("memory.views.site_settings", _ss(memory_enabled=False)),
        patch("memory.views.consolidate_memory_task") as task_mock,
    ):
        resp = client.post(reverse("memory:consolidate", args=["group/proj"]))
    task_mock.enqueue.assert_not_called()
    assert resp.status_code == 302
    msgs = [str(m) for m in get_messages(resp.wsgi_request)]
    assert any("disabled" in m.lower() for m in msgs)


@pytest.mark.django_db
def test_list_filters_repos_to_viewable(client, member_user):
    MemoryObservation.objects.create(repo_id="ok/repo", category=ObservationCategory.PITFALL, content="c" * 20)
    MemoryObservation.objects.create(repo_id="hidden/repo", category=ObservationCategory.PITFALL, content="c" * 20)
    client.force_login(member_user)

    with patch("memory.views.viewable_repo_ids", new=MagicMock(side_effect=lambda user, ids: {"ok/repo"})):
        resp = client.get(reverse("memory:list"))

    repo_ids = [row["repo_id"] for row in resp.context["repos"]]
    assert repo_ids == ["ok/repo"]


@pytest.mark.django_db
def test_detail_hidden_repo_404(client, member_user):
    MemoryObservation.objects.create(repo_id="hidden/repo", category=ObservationCategory.PITFALL, content="c" * 20)
    client.force_login(member_user)

    with patch("memory.views.can_view", new=MagicMock(return_value=False)):
        resp = client.get(reverse("memory:detail", args=["hidden/repo"]))

    assert resp.status_code == 404
