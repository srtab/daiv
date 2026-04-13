from django.db import migrations
from django.utils import timezone


def seed_email_bindings(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    UserChannelBinding = apps.get_model("notifications", "UserChannelBinding")

    now = timezone.now()
    bindings_to_create = []
    for user in User.objects.iterator():
        if not user.email:
            continue
        if UserChannelBinding.objects.filter(user=user, channel_type="email").exists():
            continue
        bindings_to_create.append(
            UserChannelBinding(user=user, channel_type="email", address=user.email, is_verified=True, verified_at=now)
        )

    if bindings_to_create:
        UserChannelBinding.objects.bulk_create(bindings_to_create)


def reverse_seed(apps, schema_editor):
    UserChannelBinding = apps.get_model("notifications", "UserChannelBinding")
    UserChannelBinding.objects.filter(channel_type="email").delete()


class Migration(migrations.Migration):
    dependencies = [("notifications", "0001_initial"), ("accounts", "0003_user_role")]

    operations = [migrations.RunPython(seed_email_bindings, reverse_seed)]
