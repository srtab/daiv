from django.conf import settings
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand, CommandError

from core.conf import settings as core_settings


class Command(BaseCommand):
    help = "Update the default Site domain from DAIV_EXTERNAL_URL."

    def handle(self, *args, **options):
        domain = core_settings.EXTERNAL_URL.host
        if not domain:
            raise CommandError("DAIV_EXTERNAL_URL has no valid host. Check your DAIV_EXTERNAL_URL setting.")

        rows = Site.objects.filter(pk=settings.SITE_ID).update(domain=domain, name="DAIV")
        if rows == 0:
            raise CommandError(f"Site with pk={settings.SITE_ID} does not exist. Run 'migrate' first.")

        self.stdout.write(self.style.SUCCESS(f"Default site updated: domain={domain}, name=DAIV"))
