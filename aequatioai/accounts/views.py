from django.conf import settings
from django.http import HttpResponseRedirect
from django.shortcuts import redirect

import requests
from allauth.account.views import LoginView as AllAuthLoginView
from allauth.account.views import LogoutView
from allauth.utils import get_request_param

from accounts.utils import get_social_app_provider


class LoginView(AllAuthLoginView):
    """View to control login with social apps."""

    def dispatch(self, request, *args, **kwargs):
        provider = get_social_app_provider(request)
        if not provider or settings.DEBUG:
            return super().dispatch(request, *args, **kwargs)
        params = {}
        next_param = get_request_param(self.request, "next")
        if next_param:
            params["next"] = next_param
        return redirect(provider.get_login_url(self.request, **params))


class SocialAccountLogoutView(LogoutView):
    def get(self, *args, **kwargs):
        response = super().get(*args, **kwargs)

        provider = get_social_app_provider(self.request)
        if not provider:
            return response

        config_response = requests.get(provider.server_url, timeout=30)
        config_response.raise_for_status()
        logout_url = config_response.json().get("end_session_endpoint")
        return HttpResponseRedirect(logout_url)
