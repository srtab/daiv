from django.core.management.base import BaseCommand, CommandError

from accounts.models import Role, User


class Command(BaseCommand):
    help = "Create the initial admin user on a fresh install."

    def add_arguments(self, parser):
        parser.add_argument("email", help="Email address for the new admin user.")

    def handle(self, *args, email, **options):
        if User.objects.filter(role=Role.ADMIN).exists():
            raise CommandError("An admin user already exists. This command only bootstraps the first one.")

        if User.objects.filter(email__iexact=email).exists():
            raise CommandError(f"A user with email '{email}' already exists. Promote them via the admin UI instead.")

        user = User.objects.create_user(username=email, email=email, role=Role.ADMIN)
        self.stdout.write(self.style.SUCCESS(f"Admin user '{email}' created (pk={user.pk})."))
        self.stdout.write("Log in via /accounts/login/ using login-by-code (a one-time code sent by email).")
