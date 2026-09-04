"""Tests for the cross-project access log view (SC-007)."""

from __future__ import annotations

from django.urls import reverse

import pytest

from codebase.models import CrossProjectAccessRecord

pytestmark = pytest.mark.django_db

THREAD = "thread-abc"


@pytest.fixture
def records(admin_user, member_user):
    CrossProjectAccessRecord.objects.create(
        thread_id=THREAD,
        acting_user=member_user,
        identity_kind=CrossProjectAccessRecord.IdentityKind.USER,
        provider="gitlab",
        target_repo_id="other-group/reachable",
        outcome=CrossProjectAccessRecord.Outcome.ALLOWED,
    )
    CrossProjectAccessRecord.objects.create(
        thread_id=THREAD,
        acting_user=member_user,
        identity_kind=CrossProjectAccessRecord.IdentityKind.USER,
        provider="gitlab",
        target_repo_id="secret-group/denied",
        outcome=CrossProjectAccessRecord.Outcome.DENIED_NO_ACCESS,
    )
    CrossProjectAccessRecord.objects.create(
        thread_id="another-thread",
        acting_user=admin_user,
        identity_kind=CrossProjectAccessRecord.IdentityKind.USER,
        provider="gitlab",
        target_repo_id="unrelated/repo",
        outcome=CrossProjectAccessRecord.Outcome.ALLOWED,
    )


def test_admin_can_answer_which_projects_under_whose_identity(admin_client, records, member_user):
    response = admin_client.get(reverse("codebase:cross-project-access"), {"thread_id": THREAD})

    assert response.status_code == 200
    content = response.content.decode()
    assert "other-group/reachable" in content
    assert "secret-group/denied" in content
    assert str(member_user) in content
    # Scoped to the thread asked about.
    assert "unrelated/repo" not in content


def test_filtering_by_outcome_narrows_the_log(admin_client, records):
    response = admin_client.get(reverse("codebase:cross-project-access"), {"outcome": "denied_no_access"})

    content = response.content.decode()
    assert "secret-group/denied" in content
    assert "other-group/reachable" not in content


def test_filtering_by_target_project_narrows_the_log(admin_client, records):
    response = admin_client.get(reverse("codebase:cross-project-access"), {"target_repo_id": "reachable"})

    content = response.content.decode()
    assert "other-group/reachable" in content
    assert "secret-group/denied" not in content


def test_an_invalid_filter_value_does_not_blank_the_list(admin_client, records):
    response = admin_client.get(reverse("codebase:cross-project-access"), {"outcome": "bogus"})

    assert response.status_code == 200
    assert "other-group/reachable" in response.content.decode()


def test_non_admins_are_refused(member_client, records):
    response = member_client.get(reverse("codebase:cross-project-access"))

    assert response.status_code == 403


def test_anonymous_users_are_redirected_to_login(client, records):
    response = client.get(reverse("codebase:cross-project-access"))

    assert response.status_code == 302


def test_the_rendered_log_carries_no_token_and_no_fetched_content(admin_client, records):
    """The record proves *that* a project was reached, never *what* was in it — and the page can
    only render what the record holds."""
    response = admin_client.get(reverse("codebase:cross-project-access"))

    content = response.content.decode()
    assert "glpat-" not in content
    assert "gho_" not in content
    for field in CrossProjectAccessRecord._meta.get_fields():
        assert "token" not in field.name
