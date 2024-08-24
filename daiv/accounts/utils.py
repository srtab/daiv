from django.core.exceptions import ImproperlyConfigured

from allauth.socialaccount.adapter import get_adapter


def get_social_app_provider(request):
    apps = get_adapter().list_apps(request)
    if not apps:
        return None
    elif len(apps) == 1:
        return get_adapter().get_provider(request, apps[0].provider_id)
    raise ImproperlyConfigured("Multiple social apps detected!")
