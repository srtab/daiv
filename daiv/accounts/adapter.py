from django.shortcuts import redirect

from allauth.account.adapter import DefaultAccountAdapter
from allauth.account.utils import perform_login
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

from .models import User


class AccountAdapter(DefaultAccountAdapter):
    """
    https://github.com/pennersr/django-allauth/blob/master/allauth/account/adapter.py
    """

    def is_open_for_signup(self, request):
        return False


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        return True

    def pre_social_login(self, request, sociallogin):
        if sociallogin.is_existing:
            return

        try:
            user = User.objects.get(email=sociallogin.account.extra_data["email"])
            sociallogin.connect(request, user)
            perform_login(request, user, email_verification=False)
            raise ImmediateHttpResponse(redirect(sociallogin.get_redirect_url(request)))

        except User.DoesNotExist:
            pass
