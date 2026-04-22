from datetime import time

from django.urls import reverse

import pytest
from notifications.choices import NotifyOn

from schedules.models import Frequency, ScheduledJob, ScheduleTemplate


@pytest.fixture
def template(admin_user):
    return ScheduleTemplate.objects.create(
        name="Nightly scan",
        description="Runs a security audit every night.",
        prompt="Scan the repo for vulnerabilities.",
        repo_id="owner/repo",
        frequency=Frequency.DAILY,
        time=time(2, 0),
        notify_on=NotifyOn.ALWAYS,
        created_by=admin_user,
    )


@pytest.mark.django_db
class TestTemplateAdminGating:
    @pytest.mark.parametrize(
        "url_name,needs_pk",
        [
            ("schedule_template_list", False),
            ("schedule_template_create", False),
            ("schedule_template_update", True),
            ("schedule_template_delete", True),
        ],
    )
    def test_member_forbidden(self, member_client, template, url_name, needs_pk):
        kwargs = {"pk": template.pk} if needs_pk else {}
        response = member_client.get(reverse(url_name, kwargs=kwargs))
        assert response.status_code == 403

    def test_admin_can_list(self, admin_client, template):
        response = admin_client.get(reverse("schedule_template_list"))
        assert response.status_code == 200
        assert template.name in response.content.decode()

    def test_admin_can_create(self, admin_client):
        response = admin_client.post(
            reverse("schedule_template_create"),
            data={
                "name": "Weekly audit",
                "description": "",
                "prompt": "Audit deps.",
                "repo_id": "",
                "ref": "",
                "frequency": Frequency.WEEKLY,
                "cron_expression": "",
                "time": "09:00",
                "notify_on": NotifyOn.NEVER,
            },
        )
        assert response.status_code == 302
        assert ScheduleTemplate.objects.filter(name="Weekly audit").exists()

    def test_admin_create_records_created_by(self, admin_client, admin_user):
        admin_client.post(
            reverse("schedule_template_create"),
            data={
                "name": "T",
                "description": "",
                "prompt": "p",
                "repo_id": "",
                "ref": "",
                "frequency": Frequency.DAILY,
                "cron_expression": "",
                "time": "09:00",
                "notify_on": NotifyOn.NEVER,
            },
        )
        tpl = ScheduleTemplate.objects.get(name="T")
        assert tpl.created_by == admin_user


@pytest.mark.django_db
class TestTemplateDeleteSafety:
    def test_deleting_template_does_not_affect_existing_schedule(self, admin_client, template, member_user):
        schedule = ScheduledJob.objects.create(
            user=member_user,
            name=template.name,
            prompt=template.prompt,
            repo_id=template.repo_id,
            frequency=template.frequency,
            time=template.time,
            notify_on=template.notify_on,
        )
        admin_client.post(reverse("schedule_template_delete", args=[template.pk]))
        assert not ScheduleTemplate.objects.filter(pk=template.pk).exists()
        assert ScheduledJob.objects.filter(pk=schedule.pk).exists()


@pytest.mark.django_db
class TestScheduleCreatePrefill:
    def test_unknown_template_pk_is_ignored(self, member_client):
        response = member_client.get(reverse("schedule_create") + "?template=99999")
        assert response.status_code == 200

    def test_non_integer_template_pk_is_ignored(self, member_client):
        response = member_client.get(reverse("schedule_create") + "?template=abc")
        assert response.status_code == 200

    def test_valid_template_prefills_form(self, member_client, template):
        response = member_client.get(reverse("schedule_create") + f"?template={template.pk}")
        assert response.status_code == 200
        body = response.content.decode()
        assert template.name in body
        assert template.prompt in body
        assert template.repo_id in body

    def test_picker_hidden_when_no_templates(self, member_client):
        ScheduleTemplate.objects.all().delete()
        response = member_client.get(reverse("schedule_create"))
        assert "Start from template" not in response.content.decode()

    def test_picker_shown_when_templates_exist(self, member_client, template):
        response = member_client.get(reverse("schedule_create"))
        assert "Start from template" in response.content.decode()

    def test_picker_hidden_on_edit(self, member_client, member_user, template):
        schedule = ScheduledJob.objects.create(
            user=member_user, name="X", prompt="p", repo_id="a/b", frequency=Frequency.DAILY, time=time(9, 0)
        )
        response = member_client.get(reverse("schedule_update", args=[schedule.pk]))
        assert "Start from template" not in response.content.decode()

    def test_custom_frequency_template_prefills_cron(self, member_client, admin_user):
        tpl = ScheduleTemplate.objects.create(
            name="Every six hours",
            prompt="Rollup.",
            repo_id="a/b",
            frequency=Frequency.CUSTOM,
            cron_expression="0 */6 * * *",
            notify_on=NotifyOn.NEVER,
            created_by=admin_user,
        )
        response = member_client.get(reverse("schedule_create") + f"?template={tpl.pk}")
        body = response.content.decode()
        assert "0 */6 * * *" in body

    def test_post_does_not_apply_template_prefill(self, member_client, template):
        response = member_client.post(
            reverse("schedule_create") + f"?template={template.pk}",
            data={
                "name": "User-chosen name",
                "prompt": "User-chosen prompt",
                "repo_id": "user/repo",
                "ref": "",
                "frequency": Frequency.DAILY,
                "cron_expression": "",
                "time": "10:00",
                "notify_on": NotifyOn.NEVER,
            },
        )
        assert response.status_code == 302
        job = ScheduledJob.objects.get(name="User-chosen name")
        assert job.prompt == "User-chosen prompt"
        assert job.repo_id == "user/repo"

    def test_malicious_template_param_does_not_leak_into_picker(self, member_client, template):
        response = member_client.get(reverse("schedule_create") + "?template=';alert(1)//")
        body = response.content.decode()
        assert "alert(1)" not in body


@pytest.mark.django_db
class TestScheduleTemplateFormConditionalClean:
    def test_non_custom_frequency_clears_cron_expression(self, admin_client):
        response = admin_client.post(
            reverse("schedule_template_create"),
            data={
                "name": "Switched",
                "description": "",
                "prompt": "p",
                "repo_id": "",
                "ref": "",
                "frequency": Frequency.DAILY,
                "cron_expression": "0 */6 * * *",
                "time": "09:00",
                "notify_on": NotifyOn.NEVER,
            },
        )
        assert response.status_code == 302
        tpl = ScheduleTemplate.objects.get(name="Switched")
        assert tpl.cron_expression == ""

    def test_hourly_frequency_clears_time(self, admin_client):
        response = admin_client.post(
            reverse("schedule_template_create"),
            data={
                "name": "Hourly tpl",
                "description": "",
                "prompt": "p",
                "repo_id": "",
                "ref": "",
                "frequency": Frequency.HOURLY,
                "cron_expression": "",
                "time": "09:00",
                "notify_on": NotifyOn.NEVER,
            },
        )
        assert response.status_code == 302
        tpl = ScheduleTemplate.objects.get(name="Hourly tpl")
        assert tpl.time is None


@pytest.mark.django_db
class TestScheduleCreateViewTemplateContext:
    """`ScheduleCreateView` serializes templates via `to_picker_dict`."""

    @pytest.fixture
    def tpl(self, admin_user):
        return ScheduleTemplate.objects.create(
            name="Weekly audit",
            description="Weekly dependency audit.",
            prompt="Do the thing.",
            repo_id="owner/repo",
            ref="main",
            frequency=Frequency.WEEKLY,
            time=time(9, 0),
            notify_on=NotifyOn.ON_SUCCESS,
            use_max=True,
            created_by=admin_user,
        )

    def test_context_uses_full_payload_shape(self, member_client, tpl):
        response = member_client.get(reverse("schedule_create"))
        assert response.status_code == 200
        [row] = response.context["schedule_templates"]
        assert row["id"] == tpl.id
        assert row["frequency_summary"] == "Weekly at 09:00"
        assert row["use_max"] is True
        assert "prompt" not in row

    def test_context_empty_when_no_templates(self, member_client):
        response = member_client.get(reverse("schedule_create"))
        assert response.status_code == 200
        assert response.context["schedule_templates"] == []


@pytest.mark.django_db
class TestScheduleFormGalleryWiring:
    """The create form renders the gallery trigger when templates exist."""

    @pytest.fixture
    def tpl(self, admin_user):
        return ScheduleTemplate.objects.create(
            name="Nightly scan",
            description="Runs nightly.",
            prompt="Scan.",
            frequency=Frequency.DAILY,
            time=time(2, 0),
            notify_on=NotifyOn.NEVER,
            created_by=admin_user,
        )

    def test_create_renders_trigger_and_gallery_when_templates_exist(self, member_client, tpl):
        response = member_client.get(reverse("schedule_create"))
        body = response.content.decode()
        assert "schedule-templates-data" in body
        assert "Browse templates" in body
        assert "open-template-gallery" in body

    def test_create_omits_trigger_when_no_templates(self, member_client):
        response = member_client.get(reverse("schedule_create"))
        body = response.content.decode()
        assert "schedule-templates-data" not in body
        assert "Browse templates" not in body

    def test_edit_omits_trigger_and_gallery(self, member_client, member_user, tpl):
        schedule = ScheduledJob(
            user=member_user,
            name="Existing",
            prompt="Hi.",
            repo_id="o/r",
            frequency=Frequency.DAILY,
            time=time(3, 0),
            notify_on=NotifyOn.NEVER,
        )
        schedule.compute_next_run()
        schedule.save()
        response = member_client.get(reverse("schedule_update", kwargs={"pk": schedule.pk}))
        body = response.content.decode()
        assert "schedule-templates-data" not in body
        assert "Browse templates" not in body
