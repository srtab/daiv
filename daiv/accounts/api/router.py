from django.db.models import Q
from django.http import HttpRequest  # noqa: TC002 - required at runtime by Django Ninja

from ninja import Router
from ninja.security import django_auth

from accounts.api.schemas import UserSearchResult
from accounts.models import User

router = Router(tags=["accounts"])

MIN_QUERY_LENGTH = 2
MAX_RESULTS = 20


@router.get("/users/search", response=list[UserSearchResult], auth=django_auth)
def search_users(request: HttpRequest, q: str = "", exclude: str = "") -> list[UserSearchResult]:
    """Search active users by username, email, or name for autocomplete.

    Excludes the requesting user and any ids passed in the ``exclude`` CSV param.
    """
    if len(q) < MIN_QUERY_LENGTH:
        return []

    exclude_ids: set[int] = {request.user.pk}
    for part in exclude.split(","):
        stripped = part.strip()
        if stripped.isdigit():
            exclude_ids.add(int(stripped))

    qs = (
        User.objects
        .filter(is_active=True)
        .filter(Q(username__icontains=q) | Q(email__icontains=q) | Q(name__icontains=q))
        .exclude(pk__in=exclude_ids)
        .order_by("username")[:MAX_RESULTS]
    )
    return [UserSearchResult(id=u.pk, username=u.username, name=u.name, email=u.email) for u in qs]
