import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db import IntegrityError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import CreateView, DeleteView, ListView, TemplateView, UpdateView

from django_filters.views import FilterView

from accounts import credentials
from accounts.emails import send_welcome_email
from accounts.filters import UserFilter
from accounts.forms import APIKeyCreateForm, UserCreateForm, UserUpdateForm
from accounts.models import APIKey, User
from codebase.conf import settings as codebase_settings
from core.mixins import AdminRequiredMixin, BreadcrumbMixin
from core.site_settings import site_settings

logger = logging.getLogger(__name__)


PICKER_USERS_LIMIT = 10
PICKER_USERS_MIN_QUERY = 2


class UserPickerView(LoginRequiredMixin, TemplateView):
    """HTMX fragment: up to ``PICKER_USERS_LIMIT`` active users matching ``?q=``.

    Excludes the requesting user and any ids passed in ``?exclude=`` (CSV).
    """

    template_name = "accounts/_user_picker_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        query = self.request.GET.get("q", "").strip()
        if len(query) < PICKER_USERS_MIN_QUERY:
            context["users"] = []
            return context

        exclude_ids: set[int] = {self.request.user.pk}
        for part in self.request.GET.get("exclude", "").split(","):
            stripped = part.strip()
            if stripped.isdigit():
                exclude_ids.add(int(stripped))

        context["users"] = (
            User.objects
            .filter(is_active=True)
            .filter(Q(username__icontains=query) | Q(email__icontains=query) | Q(name__icontains=query))
            .exclude(pk__in=exclude_ids)
            .only("pk", "username", "name", "email")
            .order_by("username")[:PICKER_USERS_LIMIT]
        )
        return context


class APIKeyListView(LoginRequiredMixin, ListView):
    model = APIKey
    template_name = "accounts/api_keys.html"
    context_object_name = "api_keys"
    paginate_by = 25

    def get_queryset(self):
        qs = super().get_queryset().order_by("revoked", "-created")
        if self.request.user.is_admin:
            return qs.select_related("user")
        return qs.filter(user=self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["new_key"] = self.request.session.pop("new_api_key", None)
        context["form"] = APIKeyCreateForm()
        context["is_admin"] = self.request.user.is_admin
        return context


class APIKeyCreateView(LoginRequiredMixin, View):
    def post(self, request):
        form = APIKeyCreateForm(request.POST)
        if not form.is_valid():
            for error in form.errors.values():
                messages.error(request, error[0])
            return redirect("api_keys")

        try:
            key_generator = APIKey.objects.key_generator
            key, prefix, hashed_key = key_generator.generate()
            APIKey.objects.create(
                user=request.user, name=form.cleaned_data["name"], prefix=prefix, hashed_key=hashed_key
            )
        except IntegrityError:
            messages.error(request, "Failed to create API key due to a conflict. Please try again.")
            return redirect("api_keys")
        except Exception:
            logger.exception("Unexpected error creating API key for user %s", request.user.pk)
            messages.error(request, "An unexpected error occurred. Please try again.")
            return redirect("api_keys")

        request.session["new_api_key"] = key
        messages.success(request, f"API key '{form.cleaned_data['name']}' created.")
        return redirect("api_keys")


class APIKeyRevokeView(LoginRequiredMixin, View):
    def post(self, request, pk):
        qs = APIKey.objects.all() if request.user.is_admin else APIKey.objects.filter(user=request.user)
        api_key = qs.filter(pk=pk).first()
        if api_key is None:
            messages.error(request, "API key not found.")
        elif api_key.revoked:
            messages.info(request, f"API key '{api_key.name}' was already revoked.")
        else:
            api_key.revoked = True
            api_key.save(update_fields=["revoked"])
            messages.success(request, f"API key '{api_key.name}' revoked.")
        return redirect("api_keys")


# ---------------------------------------------------------------------------
# User management views (admin only)
# ---------------------------------------------------------------------------


class PlatformCredentialView(LoginRequiredMixin, TemplateView):
    """The person's git platform authorisation: state, expiry and granted scopes, never the token."""

    template_name = "accounts/platform_credential.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        provider = codebase_settings.CLIENT
        context["provider"] = provider.value
        context["supported"] = provider in credentials.OAUTH_CAPABLE_PLATFORMS
        context["cross_project_enabled"] = bool(site_settings.cross_project_access_enabled)
        if context["supported"]:
            context["status"] = credentials.status(user_id=self.request.user.pk, provider=provider)
            context["connect_url"] = reverse(f"{provider.value}_login")
        return context


class PlatformCredentialRevokeView(LoginRequiredMixin, View):
    """Disconnect: clear both secrets and mark the grant revoked. Only ever one's own."""

    def post(self, request):
        provider = codebase_settings.CLIENT
        if provider not in credentials.OAUTH_CAPABLE_PLATFORMS:
            messages.error(request, _("This deployment has no git platform authorisation to disconnect."))
            return redirect("platform_credential")

        if credentials.revoke(user_id=request.user.pk, provider=provider):
            messages.success(request, _("Disconnected your %(provider)s authorisation.") % {"provider": provider.value})
        else:
            messages.info(
                request, _("There was no %(provider)s authorisation to disconnect.") % {"provider": provider.value}
            )
        return redirect("platform_credential")


class PlatformCredentialReconnectView(LoginRequiredMixin, View):
    """Start the OAuth flow as an explicit re-authorisation.

    ``credentials.store`` never resurrects a revoked row, so this POST is the only way back: the sign-in that
    completes the flow clears the revoked grant first (``accounts.signals``). An abandoned flow leaves it revoked.
    """

    def post(self, request):
        provider = codebase_settings.CLIENT
        if provider not in credentials.OAUTH_CAPABLE_PLATFORMS:
            messages.error(request, _("This deployment has no git platform authorisation to grant."))
            return redirect("platform_credential")

        request.session[credentials.REAUTHORISE_SESSION_KEY] = request.user.pk
        return redirect(f"{provider.value}_login")


class UserListView(AdminRequiredMixin, FilterView):
    model = User
    filterset_class = UserFilter
    template_name = "accounts/users.html"
    context_object_name = "users"
    ordering = ["-date_joined"]
    paginate_by = 25
    # Invalid URL params (e.g. ?role=bogus) drop silently instead of blanking the list.
    strict = False

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = context["filter"].form
        cleaned = form.cleaned_data if form.is_valid() else {}
        context["search_query"] = cleaned.get("q") or ""
        context["current_role"] = cleaned.get("role") or ""
        return context


class UserCreateView(BreadcrumbMixin, AdminRequiredMixin, CreateView):
    model = User
    form_class = UserCreateForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("user_list")

    def form_valid(self, form):
        response = super().form_valid(form)
        assert isinstance(self.object, User)
        login_url = self.request.build_absolute_uri(reverse("account_login"))
        email_sent = send_welcome_email(self.object, login_url)
        if email_sent:
            messages.success(self.request, f"User '{self.object.email}' created. A welcome email has been sent.")
        else:
            messages.warning(
                self.request,
                f"User '{self.object.email}' created, but the welcome email could not be sent."
                " Please notify the user manually.",
            )
        return response

    def get_breadcrumbs(self):
        return [{"label": "Users", "url": reverse("user_list")}, {"label": "New user", "url": None}]


class UserUpdateView(BreadcrumbMixin, SuccessMessageMixin, AdminRequiredMixin, UpdateView):
    model = User
    form_class = UserUpdateForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("user_list")
    success_message = "User '%(email)s' updated."

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["requesting_user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        from allauth.mfa.models import Authenticator

        context = super().get_context_data(**kwargs)
        context["passkeys"] = Authenticator.objects.filter(
            user_id=self.object.pk, type=Authenticator.Type.WEBAUTHN
        ).order_by("created_at")
        return context

    def get_breadcrumbs(self):
        return [{"label": "Users", "url": reverse("user_list")}, {"label": self.object.email, "url": None}]


class UserDeleteView(BreadcrumbMixin, SuccessMessageMixin, AdminRequiredMixin, DeleteView):
    model = User
    template_name = "accounts/user_confirm_delete.html"
    success_url = reverse_lazy("user_list")
    success_message = "User deleted."

    def post(self, request, *args, **kwargs):
        user = self.get_object()
        if user.pk == request.user.pk:
            messages.error(request, "You cannot delete your own account.")
            return redirect("user_list")
        if user.is_last_active_admin():
            messages.error(request, "Cannot delete the last admin. Promote another user first.")
            return redirect("user_list")
        return super().post(request, *args, **kwargs)

    def get_success_message(self, cleaned_data: dict) -> str:
        return f"User '{self.object.email}' deleted."

    def get_breadcrumbs(self):
        return [
            {"label": "Users", "url": reverse("user_list")},
            {"label": self.object.email, "url": reverse("user_update", args=[self.object.pk])},
            {"label": "Delete", "url": None},
        ]


def _remove_user_passkey(request, user, authenticator, email=None):
    """
    Delete a user's WebAuthn authenticator on their behalf (admin action) and notify
    the owner — one email per removed key, naming the key.

    allauth's ``delete_and_cleanup`` is not used: both the notification and the
    ``authenticator_removed`` signal it emits carry ``request.user``, which is the
    admin rather than the passkey owner. Recovery codes need no cleanup here because
    ``MFA_SUPPORTED_TYPES`` is webauthn-only.
    """
    from allauth.account.adapter import get_adapter as get_account_adapter
    from allauth.mfa import signals
    from allauth.mfa.models import Authenticator

    name = str(authenticator)
    authenticator.delete()
    signals.authenticator_removed.send(sender=Authenticator, request=request, user=user, authenticator=authenticator)
    get_account_adapter(request).send_notification_mail(
        "mfa/email/webauthn_removed", user, context={"passkey_name": name}, email=email
    )
    return name


class UserPasskeyRemoveView(AdminRequiredMixin, View):
    """Remove a single passkey belonging to another user."""

    def post(self, request, pk, authenticator_pk):
        from allauth.mfa.models import Authenticator

        user = get_object_or_404(User, pk=pk)
        authenticator = get_object_or_404(
            Authenticator, pk=authenticator_pk, user_id=user.pk, type=Authenticator.Type.WEBAUTHN
        )
        name = _remove_user_passkey(request, user, authenticator)
        messages.success(request, f"Passkey '{name}' removed from '{user.email}'. The user has been notified.")
        return redirect("user_update", pk=user.pk)


class UserPasskeysResetView(AdminRequiredMixin, View):
    """Remove all passkeys belonging to another user."""

    def post(self, request, pk):
        from allauth.account.models import EmailAddress
        from allauth.mfa.models import Authenticator

        user = get_object_or_404(User, pk=pk)
        authenticators = list(
            Authenticator.objects.filter(user_id=user.pk, type=Authenticator.Type.WEBAUTHN).order_by("created_at")
        )
        if not authenticators:
            messages.info(request, f"'{user.email}' has no passkeys to remove.")
            return redirect("user_update", pk=user.pk)
        # One notification per removed key; the recipient is resolved once, not once per key.
        email = EmailAddress.objects.get_primary_email(user) or user.email
        for authenticator in authenticators:
            _remove_user_passkey(request, user, authenticator, email=email)
        messages.success(
            request, f"Removed {len(authenticators)} passkey(s) from '{user.email}'. The user has been notified."
        )
        return redirect("user_update", pk=user.pk)
