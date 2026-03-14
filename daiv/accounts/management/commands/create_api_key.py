from django.core.management.base import BaseCommand, CommandError

from accounts.models import APIKey, User


class Command(BaseCommand):
    help = "Create an API key for a user."

    def add_arguments(self, parser):
        parser.add_argument("username", type=str, help="Username of the user to create the API key for.")
        parser.add_argument("--name", type=str, help="Name for the API key.", default="")
        parser.add_argument(
            "--expires-at",
            type=str,
            help="Expiration date for the API key in ISO 8601 format (e.g. 2026-12-31T23:59:59).",
            default=None,
        )

    def handle(self, *args, **options):
        from datetime import datetime

        from django.utils import timezone

        try:
            user = User.objects.get(username=options["username"])
        except User.DoesNotExist as err:
            raise CommandError(f"User '{options['username']}' does not exist.") from err

        expires_at = None
        if options["expires_at"]:
            try:
                expires_at = datetime.fromisoformat(options["expires_at"])
                if timezone.is_naive(expires_at):
                    expires_at = timezone.make_aware(expires_at)
            except ValueError as err:
                raise CommandError(f"Invalid date format: '{options['expires_at']}'. Use ISO 8601 format.") from err

        from asgiref.sync import async_to_sync

        _, key = async_to_sync(APIKey.objects.create_key)(user=user, name=options["name"], expires_at=expires_at)

        print(f"API key created: {key}")  # noqa: T201
        print("Store this key securely — it cannot be retrieved once this command exits.")  # noqa: T201
