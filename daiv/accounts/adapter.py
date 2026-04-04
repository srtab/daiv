import logging

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

logger = logging.getLogger(__name__)


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        """Disable standard email/password signup. Users are created by admins."""
        return False


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        """
        Block social signup for emails not already registered (users must be created by an admin first).

        When a user authenticates via GitHub/GitLab, allauth will auto-connect the social
        account to the existing user via SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT.
        """
        from accounts.models import User

        email = sociallogin.user.email
        provider = getattr(sociallogin.account, "provider", "unknown") if sociallogin.account else "unknown"
        if not email:
            logger.warning("Social signup denied: no email provided by %s provider", provider)
            return False
        if User.objects.filter(email__iexact=email).exists():
            return True
        logger.info("Social signup denied for unregistered email %s via %s", email, provider)
        return False
