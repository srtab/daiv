from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.http import HttpRequest


def is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"
