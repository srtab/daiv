from django.db import migrations, models


def set_existing_users_to_admin(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.all().update(role="admin")


def reverse_set_existing_users_to_admin(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.all().update(role="member")


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_apikey")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="role",
            field=models.CharField(
                choices=[("admin", "Admin"), ("member", "Member")], default="member", max_length=10, verbose_name="role"
            ),
        ),
        migrations.RunPython(set_existing_users_to_admin, reverse_set_existing_users_to_admin),
    ]
