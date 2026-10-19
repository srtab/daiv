from typing import TypedDict

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.views.generic.base import ContextMixin


class Breadcrumb(TypedDict):
    label: str
    url: str | None


class AdminRequiredMixin(LoginRequiredMixin):
    """Requires the user to be logged in and have the admin role."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not request.user.is_admin:
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class BreadcrumbMixin(ContextMixin):
    """Injects a ``breadcrumbs`` list into the template context.

    Views with static crumbs can set the class attribute; views that need dynamic crumbs
    (e.g. derived from ``self.object``) should override ``get_breadcrumbs``. The last
    crumb conventionally has ``url=None`` to render as the current page.
    """

    breadcrumbs: list[Breadcrumb] | None = None

    def get_breadcrumbs(self) -> list[Breadcrumb]:
        return self.breadcrumbs or []

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["breadcrumbs"] = self.get_breadcrumbs()
        return context
