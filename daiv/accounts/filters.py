from __future__ import annotations

from django.db.models import Q

import django_filters

from accounts.models import Role, User


class UserFilter(django_filters.FilterSet):
    q = django_filters.CharFilter(method="filter_search")
    role = django_filters.ChoiceFilter(choices=Role.choices)

    class Meta:
        model = User
        # All filters are declared above; disable auto-generation from model fields.
        fields: list[str] = []

    def filter_search(self, queryset, name, value):
        value = (value or "").strip()
        if not value:
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(email__icontains=value))
