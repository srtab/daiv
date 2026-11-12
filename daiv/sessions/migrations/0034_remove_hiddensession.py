from django.db import migrations, models


def archive_hidden_sessions(apps, schema_editor):
    Session = apps.get_model("agent_sessions", "Session")
    HiddenSession = apps.get_model("agent_sessions", "HiddenSession")
    first_hidden = HiddenSession.objects.filter(session=models.OuterRef("pk")).order_by("hidden_at")
    Session.objects.filter(archived_at__isnull=True).filter(models.Exists(first_hidden)).update(
        archived_at=models.Subquery(first_hidden.values("hidden_at")[:1]), archive_pending=False
    )


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0033_archive_pending")]

    operations = [
        migrations.RunPython(archive_hidden_sessions, migrations.RunPython.noop),
        migrations.DeleteModel(name="HiddenSession"),
    ]
