from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta

from django.urls import reverse
from django.utils import timezone

import pytest
from allauth.socialaccount.models import SocialAccount
from sessions import views as views_module
from sessions.models import Run, RunArtifact, RunStatus, Session, SessionOrigin

from codebase.models import RepositoryAccess
from tests.unit_tests.sessions.conftest import commit_revision, make_artifact

pytestmark = pytest.mark.django_db


def _create_session(**kwargs) -> Session:
    defaults = {
        "thread_id": str(uuid.uuid4()),
        "origin": SessionOrigin.UI_JOB,
        "repo_id": "group/project",
        "ref": "main",
    }
    defaults.update(kwargs)
    return Session.objects.create(**defaults)


def _create_run(session: Session, **kwargs) -> Run:
    defaults = {
        "session": session,
        "trigger_type": SessionOrigin.UI_JOB,
        "repo_id": session.repo_id,
        "status": RunStatus.SUCCESSFUL,
        "user": session.user,
    }
    defaults.update(kwargs)
    return Run.objects.create(**defaults)


def _own_artifact(user, **artifact_kwargs) -> RunArtifact:
    return make_artifact(_create_run(_create_session(user=user)), **artifact_kwargs)


def test_detail_requires_login(client, member_user):
    artifact = _own_artifact(member_user)
    resp = client.get(artifact.get_absolute_url())
    assert resp.status_code == 302
    assert "login" in resp["Location"].lower()


def test_detail_404_for_other_users_artifact(member_client, other_user):
    artifact = _own_artifact(other_user)
    assert member_client.get(artifact.get_absolute_url()).status_code == 404
    assert member_client.get(artifact.get_raw_url()).status_code == 404


@pytest.mark.parametrize("url_name", ["session_artifact_detail", "session_artifact_raw"])
def test_404_when_thread_does_not_own_artifact(member_client, member_user, url_name):
    artifact = _own_artifact(member_user)
    other_session = _create_session(user=member_user)
    url = reverse(url_name, kwargs={"thread_id": other_session.thread_id, "pk": artifact.pk})
    assert member_client.get(url).status_code == 404


def test_session_owner_opens_artifact_of_a_run_someone_else_triggered(member_client, member_user, other_user):
    session = _create_session(user=member_user)
    artifact = make_artifact(_create_run(session, user=other_user))

    assert member_client.get(artifact.get_absolute_url()).status_code == 200
    assert member_client.get(artifact.get_raw_url()).status_code == 200


def test_detail_renders_markdown_inline(member_client, member_user):
    artifact = _own_artifact(
        member_user, filename="findings.md", content=b"# Findings\n\n<script>x</script>", title="Findings"
    )

    resp = member_client.get(artifact.get_absolute_url())

    assert resp.status_code == 200
    assert resp.context["artifact"].kind == "markdown"
    html = resp.content.decode()
    assert "<h1>Findings</h1>" in html
    assert "<script>x</script>" not in html
    assert [c["label"] for c in resp.context["breadcrumbs"]] == ["Artifacts", "Findings"]


def test_detail_embeds_html_in_sandboxed_iframe(member_client, member_user):
    artifact = _own_artifact(member_user, filename="audit.html", content=b"<h1>Audit</h1><script>alert(1)</script>")

    resp = member_client.get(artifact.get_absolute_url())

    html = resp.content.decode()
    assert resp.context["artifact"].kind == "html"
    assert "<h1>Audit</h1>" not in html
    assert f'<iframe src="{artifact.get_raw_url()}"' in html
    assert 'sandbox="allow-scripts allow-popups"' in html


def test_detail_renders_text_escaped(member_client, member_user):
    artifact = _own_artifact(member_user, filename="data.csv", content=b"a,b\n1,<2>")

    resp = member_client.get(artifact.get_absolute_url())

    assert resp.context["artifact"].kind == "text"
    assert "1,&lt;2&gt;" in resp.content.decode()


def test_detail_renders_image_from_raw_endpoint(member_client, member_user):
    artifact = _own_artifact(member_user, filename="chart.png", content=b"\x89PNG")

    resp = member_client.get(artifact.get_absolute_url())

    assert resp.context["artifact"].kind == "image"
    assert f'<img src="{artifact.get_raw_url()}"' in resp.content.decode()


def test_detail_offers_download_for_unpreviewable_types(member_client, member_user):
    artifact = _own_artifact(member_user, filename="report.pdf", content=b"%PDF-1.4")

    resp = member_client.get(artifact.get_absolute_url())

    assert resp.context["artifact"].kind == "other"
    assert resp.context["text"] is None
    assert "not previewed" in resp.content.decode()


def test_detail_large_text_is_flagged_too_large_not_unpreviewable(member_client, member_user, monkeypatch):
    monkeypatch.setattr(views_module, "ARTIFACT_INLINE_TEXT_MAX_BYTES", 4)
    artifact = _own_artifact(member_user, filename="big.md", content=b"# too big")

    resp = member_client.get(artifact.get_absolute_url())

    assert resp.context["artifact"].kind == "markdown"
    assert resp.context["too_large"] is True
    assert resp.context["text"] is None
    html = resp.content.decode()
    assert "too large to preview" in html
    assert "not previewed" not in html


@pytest.mark.parametrize(
    ("filename", "state", "download_links", "raw_links"),
    [
        ("notes.md", "previewable", 1, 1),
        ("report.pdf", "unpreviewable", 1, 0),
        ("big.md", "too_large", 1, 0),
        ("gone.md", "unavailable", 0, 0),
    ],
)
def test_detail_offers_each_file_link_at_most_once(
    member_client, member_user, monkeypatch, filename, state, download_links, raw_links
):
    if state == "too_large":
        monkeypatch.setattr(views_module, "ARTIFACT_INLINE_TEXT_MAX_BYTES", 4)
    artifact = _own_artifact(member_user, filename=filename, content=b"# content")
    if state == "unavailable":
        artifact.file.storage.delete(artifact.file.name)

    html = member_client.get(artifact.get_absolute_url()).content.decode()

    assert html.count(f'href="{artifact.get_download_url()}"') == download_links
    assert html.count(f'href="{artifact.get_raw_url()}"') == raw_links


def test_missing_file_renders_unavailable_and_raw_404s(member_client, member_user, caplog):
    artifact = _own_artifact(member_user, filename="audit.html", content=b"<p>x</p>")
    artifact.file.storage.delete(artifact.file.name)

    detail = member_client.get(artifact.get_absolute_url())
    raw = member_client.get(artifact.get_raw_url())

    assert detail.status_code == 200
    assert detail.context["unavailable"] is True
    assert "no longer available" in detail.content.decode()
    assert "<iframe" not in detail.content.decode()
    assert raw.status_code == 404
    assert "MEDIA_ROOT" in caplog.text


def _revise_after_lookup(monkeypatch, view_class, artifact: RunArtifact, content: bytes) -> None:
    """Commit a revision of ``artifact`` right after ``view_class`` loads it, as a concurrent publish would."""
    load = view_class.get_object

    def load_then_revise(view, queryset=None):
        loaded = load(view, queryset)
        commit_revision(loaded, content)
        return loaded

    monkeypatch.setattr(view_class, "get_object", load_then_revise)


def test_detail_renders_a_revision_that_commits_after_the_lookup(member_client, member_user, monkeypatch):
    artifact = _own_artifact(member_user, filename="audit.md", content=b"# v1")
    _revise_after_lookup(monkeypatch, views_module.RunArtifactDetailView, artifact, b"# v2")

    resp = member_client.get(artifact.get_absolute_url())

    assert (resp.status_code, resp.context["unavailable"], resp.context["text"]) == (200, False, "# v2")


def test_raw_streams_a_revision_that_commits_after_the_lookup(member_client, member_user, monkeypatch):
    artifact = _own_artifact(member_user, filename="audit.md", content=b"# v1")
    _revise_after_lookup(monkeypatch, views_module.RunArtifactRawView, artifact, b"# v2")

    resp = member_client.get(artifact.get_raw_url())

    assert resp.status_code == 200
    assert b"".join(resp.streaming_content) == b"# v2"


def test_raw_requires_login(client, member_user):
    artifact = _own_artifact(member_user)
    resp = client.get(artifact.get_raw_url())
    assert resp.status_code == 302


def test_raw_streams_bytes_as_utf8_text(member_client, member_user):
    artifact = _own_artifact(member_user, filename="audit.html", content="<h1>Relatório</h1>".encode())

    resp = member_client.get(artifact.get_raw_url())

    assert resp.status_code == 200
    assert b"".join(resp.streaming_content) == "<h1>Relatório</h1>".encode()
    assert resp["Content-Type"] == "text/html; charset=utf-8"
    assert resp["Content-Disposition"] == 'inline; filename="audit.html"'


@pytest.mark.parametrize(
    ("filename", "query"), [("audit.html", ""), ("chart.svg", ""), ("chart.png", ""), ("audit.html", "?download=1")]
)
def test_raw_always_sends_the_sandbox_policy(member_client, member_user, filename, query):
    artifact = _own_artifact(member_user, filename=filename, content=b"<svg/>")

    resp = member_client.get(artifact.get_raw_url() + query)

    csp = resp["Content-Security-Policy"]
    assert csp.startswith("sandbox allow-scripts allow-popups;")
    assert "frame-ancestors 'self'" in csp
    assert "connect-src 'none'" in csp
    assert resp["X-Content-Type-Options"] == "nosniff"
    assert resp["X-Frame-Options"] == "SAMEORIGIN"
    assert resp["Referrer-Policy"] == "no-referrer"
    assert resp["Cache-Control"] == "private, no-store"


def test_raw_binary_type_carries_no_charset(member_client, member_user):
    artifact = _own_artifact(member_user, filename="chart.png", content=b"\x89PNG")

    assert member_client.get(artifact.get_raw_url())["Content-Type"] == "image/png"


def test_raw_download_flag_forces_attachment(member_client, member_user):
    artifact = _own_artifact(member_user, filename="data.csv", content=b"a,b")

    resp = member_client.get(artifact.get_raw_url() + "?download=1")

    assert resp["Content-Disposition"] == 'attachment; filename="data.csv"'
    assert resp["Content-Type"] == "text/csv; charset=utf-8"
    assert b"".join(resp.streaming_content) == b"a,b"


def test_raw_visible_to_admin(admin_client, member_user):
    artifact = _own_artifact(member_user)
    assert admin_client.get(artifact.get_raw_url()).status_code == 200


def test_raw_and_detail_404_when_the_session_holds_another_persons_cross_project_results(member_client, member_user):
    artifact = _own_artifact(member_user)
    Session.objects.filter(pk=artifact.run.session_id).update(cross_project_user_ids=[member_user.pk + 1])

    assert member_client.get(artifact.get_raw_url()).status_code == 404
    assert member_client.get(artifact.get_absolute_url()).status_code == 404
    assert artifact not in member_client.get(reverse("artifact_list")).context["artifacts"]


def test_detail_breadcrumbs_link_to_artifact_list(member_client, member_user):
    artifact = _own_artifact(member_user, filename="findings.md", title="Findings")

    resp = member_client.get(artifact.get_absolute_url())

    assert resp.context["breadcrumbs"] == [
        {"label": "Artifacts", "url": reverse("artifact_list")},
        {"label": "Findings", "url": None},
    ]


@pytest.mark.parametrize("updated", [False, True])
def test_detail_says_when_a_revised_artifact_was_updated(member_client, member_user, updated):
    artifact = _own_artifact(member_user)
    RunArtifact.objects.filter(pk=artifact.pk).update(updated_at=timezone.now() if updated else None)

    html = member_client.get(artifact.get_absolute_url()).content.decode()

    assert "Published" in html
    assert ("Updated" in html) is updated


def test_detail_links_back_to_its_session_and_repo(member_client, member_user):
    session = _create_session(user=member_user, title="Nightly audit", repo_id="acme/api")
    artifact = make_artifact(_create_run(session), filename="audit.md")

    html = member_client.get(artifact.get_absolute_url()).content.decode()

    session_url = re.escape(reverse("session_detail", kwargs={"thread_id": session.thread_id}))
    assert re.search(rf'href="{session_url}#run-{artifact.run_id}"[^>]*>\s*Nightly audit\s*</a>', html)
    assert "acme/api" in html


def test_detail_embeds_markdown_source_for_copy(member_client, member_user):
    artifact = _own_artifact(member_user, filename="notes.md", content=b"# Notes\n\n- a <b>")

    html = member_client.get(artifact.get_absolute_url()).content.decode()

    assert '<script id="artifact-source" type="application/json">"# Notes\\n\\n- a \\u003Cb\\u003E"</script>' in html


def _grant_read_access(user, repo_id: str) -> None:
    SocialAccount.objects.get_or_create(user=user, provider="gitlab", uid=str(user.pk))
    RepositoryAccess.objects.create(
        provider="gitlab",
        uid=str(user.pk),
        username=user.username,
        repo_id=repo_id,
        access_level="read",
        synced_at=timezone.now(),
    )


class TestArtifactListView:
    def test_requires_login(self, client, member_user):
        resp = client.get(reverse("artifact_list"))
        assert resp.status_code == 302
        assert "login" in resp["Location"].lower()

    def test_owner_sees_own_artifact(self, member_client, member_user):
        artifact = _own_artifact(member_user)

        resp = member_client.get(reverse("artifact_list"))

        assert resp.status_code == 200
        assert list(resp.context["artifacts"]) == [artifact]

    def test_user_sees_artifact_of_a_readable_repo_they_dont_own(self, member_client, member_user, other_user):
        _grant_read_access(member_user, "readable/repo")
        session = _create_session(user=other_user, repo_id="readable/repo")
        artifact = make_artifact(_create_run(session))

        resp = member_client.get(reverse("artifact_list"))

        assert list(resp.context["artifacts"]) == [artifact]

    def test_other_users_artifact_in_unreadable_repo_is_hidden(self, member_client, other_user):
        session = _create_session(user=other_user, repo_id="unreadable/repo")
        make_artifact(_create_run(session))

        resp = member_client.get(reverse("artifact_list"))

        assert list(resp.context["artifacts"]) == []

    def test_admin_sees_every_artifact(self, admin_client, member_user, other_user):
        mine = _own_artifact(member_user)
        theirs = _own_artifact(other_user)

        resp = admin_client.get(reverse("artifact_list"))

        pks = {a.pk for a in resp.context["artifacts"]}
        assert {mine.pk, theirs.pk} <= pks

    def test_orders_newest_first(self, member_client, member_user):
        session = _create_session(user=member_user)
        run = _create_run(session)
        older = make_artifact(run, filename="a.md", created_at=timezone.now() - timedelta(minutes=5))
        newer = make_artifact(run, filename="b.md")

        resp = member_client.get(reverse("artifact_list"))

        assert list(resp.context["artifacts"]) == [newer, older]

    def test_paginates_at_25(self, member_client, member_user):
        session = _create_session(user=member_user)
        run = _create_run(session)
        for i in range(26):
            make_artifact(run, filename=f"f{i}.md")

        resp = member_client.get(reverse("artifact_list"))

        assert len(resp.context["artifacts"]) == 25
        assert resp.context["page_obj"].has_next()

    def test_htmx_request_uses_results_fragment(self, member_client, member_user):
        _own_artifact(member_user)

        resp = member_client.get(reverse("artifact_list"), HTTP_HX_REQUEST="true")

        names = [t.name for t in resp.templates if t.name]
        assert "sessions/_artifact_results.html" in names
        assert "sessions/artifact_list.html" not in names

    def test_full_page_request_uses_list_template(self, member_client, member_user):
        _own_artifact(member_user)

        resp = member_client.get(reverse("artifact_list"))

        assert "sessions/artifact_list.html" in [t.name for t in resp.templates if t.name]

    def test_repos_context_scoped_to_viewer(self, member_client, member_user, other_user):
        make_artifact(_create_run(_create_session(user=member_user, repo_id="mine/repo")))
        make_artifact(_create_run(_create_session(user=other_user, repo_id="theirs/repo")))

        resp = member_client.get(reverse("artifact_list"))

        assert resp.context["repos"] == ["mine/repo"]

    def test_repos_context_not_narrowed_by_active_filters(self, member_client, member_user):
        session = _create_session(user=member_user, repo_id="mine/repo")
        run = _create_run(session)
        make_artifact(run, filename="a.md")
        make_artifact(run, filename="b.html", content=b"<h1>x</h1>")

        resp = member_client.get(reverse("artifact_list"), {"kind": "markdown"})

        assert resp.context["repos"] == ["mine/repo"]
        assert len(resp.context["artifacts"]) == 1

    def test_empty_state_first_use_offers_a_new_session(self, member_client, member_user):
        resp = member_client.get(reverse("artifact_list"), HTTP_HX_REQUEST="true")

        assert f'href="{reverse("session_new")}"' in resp.content.decode()

    def test_groups_rows_under_date_headers_and_dates_older_rows(self, member_client, member_user):
        run = _create_run(_create_session(user=member_user))
        make_artifact(run, filename="new.md")
        make_artifact(run, filename="newer.md")
        make_artifact(run, filename="old.md", created_at=timezone.make_aware(datetime(2024, 3, 9, 12, 0)))

        html = member_client.get(reverse("artifact_list"), HTTP_HX_REQUEST="true").content.decode()

        headers = re.findall(r'class="session-group-header">\s*<span>([^<]+)</span>', html)
        assert headers == ["Today", "March 2024"]
        assert re.search(r'<time[^>]*datetime="2024-03-09[^"]*"[^>]*>\s*Mar 9\s*</time>', html)

    def test_empty_state_filtered(self, member_client, member_user):
        _own_artifact(member_user, filename="a.md")

        resp = member_client.get(reverse("artifact_list"), {"q": "zzz-no-match"}, HTTP_HX_REQUEST="true")

        html = resp.content.decode()
        assert "No artifacts match these filters." in html
        clear_all_url = re.escape(reverse("artifact_list"))
        assert re.search(rf'href="{clear_all_url}"[^>]*>\s*Clear all\s*</a>', html)

    def test_row_renders_session_title_kind_and_download_link(self, member_client, member_user):
        session = _create_session(user=member_user, title="My debug session", repo_id="mine/repo")
        run = _create_run(session)
        artifact = make_artifact(run, filename="findings.md", title="Findings")

        resp = member_client.get(reverse("artifact_list"), HTTP_HX_REQUEST="true")
        html = resp.content.decode()

        assert f'href="{artifact.get_absolute_url()}"' in html
        assert "Findings" in html
        assert re.search(r'class="meta-pill[^"]*">\s*Markdown\s*</span>', html)
        assert "My debug session" in html
        assert "mine/repo" in html
        assert f'href="{artifact.get_download_url()}"' in html

    def test_row_shows_repo_beside_an_untitled_session_link(self, member_client, member_user):
        session = _create_session(user=member_user, title="", repo_id="untitled/repo")
        artifact = make_artifact(_create_run(session))

        resp = member_client.get(reverse("artifact_list"), HTTP_HX_REQUEST="true")
        html = resp.content.decode()

        session_url = re.escape(reverse("session_detail", kwargs={"thread_id": session.thread_id}))
        assert re.search(rf'href="{session_url}#run-{artifact.run_id}"[^>]*>\s*<em[^>]*>generating title…</em>', html)
        assert re.search(r'class="session-repo[^"]*"[^>]*>.*?untitled/repo', html, re.S)

    def test_row_without_session_title_or_repo_has_no_empty_link_or_bare_repo_pill(self, member_client, member_user):
        session = _create_session(user=member_user, title="", repo_id="")
        artifact = make_artifact(_create_run(session))

        resp = member_client.get(reverse("artifact_list"), HTTP_HX_REQUEST="true")
        html = resp.content.decode()

        session_url = re.escape(reverse("session_detail", kwargs={"thread_id": session.thread_id}))
        assert re.search(rf'href="{session_url}#run-{artifact.run_id}"[^>]*>\s*<em[^>]*>generating title…</em>', html)
        assert "session-repo" not in html

    def test_repo_filter_menu_keeps_the_other_active_filters(self, member_client, member_user):
        _own_artifact(member_user)

        html = member_client.get(reverse("artifact_list"), {"kind": "markdown"}).content.decode()

        assert 'href="?kind=markdown&amp;repo=group/project"' in html

    @pytest.mark.parametrize(
        ("query", "expected"),
        [({}, ""), ({"repo": "group/project"}, "group/project"), ({"repo": "nope/nope"}, "nope/nope")],
    )
    def test_current_repo_echoes_the_repo_filter(self, member_client, member_user, query, expected):
        _own_artifact(member_user)

        resp = member_client.get(reverse("artifact_list"), query)

        assert resp.context["current_repo"] == expected
