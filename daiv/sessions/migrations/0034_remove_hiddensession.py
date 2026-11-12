from django.db import migrations, models


def archive_hidden_sessions(apps, schema_editor):
    Session = apps.get_model("agent_sessions", "Session")
    HiddenSession = apps.get_model("agent_sessions", "HiddenSession")
    owner_hid = HiddenSession.objects.filter(session=models.OuterRef("pk"), user=models.OuterRef("user"))
    Session.objects.filter(archived_at__isnull=True).filter(models.Exists(owner_hid)).update(
        archived_at=models.Subquery(owner_hid.values("hidden_at")[:1]), archive_pending=False
    )


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0033_archive_pending")]

    operations = [
        migrations.RunPython(archive_hidden_sessions, migrations.RunPython.noop),
        migrations.DeleteModel(name="HiddenSession"),
    ]
