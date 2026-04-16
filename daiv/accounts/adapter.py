import logging

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialApp

from codebase.base import GitPlatform
from codebase.conf import settings as codebase_settings
from core.site_settings import site_settings

logger = logging.getLogger(__name__)

_PLATFORM_TO_PROVIDER: dict[GitPlatform, str] = {GitPlatform.GITLAB: "gitlab", GitPlatform.GITHUB: "github"}


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        """Disable standard email/password signup. Users are created by admins."""
        return False


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def save_user(self, request, sociallogin, form=None):
        """
        Assign admin role to the first user created via social login (bootstrapping).

        Uses the absence of any admin user as the trigger rather than the absence of any user,
        so that concurrent first logins result in multiple admins (safe) rather than zero admins.
        """
        from accounts.models import Role, User

        user = super().save_user(request, sociallogin, form)
        if not User.objects.filter(role=Role.ADMIN).exists():
            user.role = Role.ADMIN
            try:
                user.save(update_fields=["role"])
            except Exception:
                logger.exception(
                    "Failed to assign admin role to first user (pk=%s). "
                    "Manually set this user's role to 'admin' in the database.",
                    user.pk,
                )
                raise
            logger.info("Bootstrapping: assigned admin role to first user %s (pk=%s)", user.email, user.pk)
        return user

    def list_apps(self, request, provider=None, client_id=None):
        expected_provider = _PLATFORM_TO_PROVIDER.get(codebase_settings.CLIENT)
        if expected_provider is None:
            return []
        if provider and provider != expected_provider:
            return []

        client_id_value = site_settings.auth_client_id
        secret = site_settings.auth_client_secret
        if not client_id_value or secret is None:
            return []

        app_settings: dict[str, str] = {}
        if expected_provider == "gitlab":
            app_settings = {
                "gitlab_url": site_settings.auth_gitlab_url or "https://gitlab.com",
                "gitlab_server_url": site_settings.auth_gitlab_server_url or "",
            }

        secret_value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
        return [
            SocialApp(
                provider=expected_provider,
                name=f"{expected_provider.capitalize()} (SiteConfiguration)",
                client_id=client_id_value,
                secret=secret_value,
                settings=app_settings,
            )
        ]

    def is_open_for_signup(self, request, sociallogin):
        """
        Block social signup for emails not already registered (users must be created by an admin first).

        On a fresh install (no users exist), the first social login is allowed to bootstrap
        the initial admin account. After that, users must be created by an admin.

        When a user authenticates via GitHub/GitLab, allauth will auto-connect the social
        account to the existing user via SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT.
        """
        from accounts.models import User

        email = sociallogin.user.email
        provider = getattr(sociallogin.account, "provider", "unknown") if sociallogin.account else "unknown"
        if not email:
            logger.warning("Social signup denied: no email provided by %s provider", provider)
            return False
        # Allow the first user to sign up on a fresh install (bootstrapping).
        if not User.objects.exists():
            logger.info("Bootstrapping: allowing first social signup for %s via %s", email, provider)
            return True
        if User.objects.filter(email__iexact=email).exists():
            return True
        logger.info("Social signup denied for unregistered email %s via %s", email, provider)
        return False
