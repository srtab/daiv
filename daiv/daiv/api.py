from jobs.api.views import jobs_router
from mcp_server.api.views import oauth_router
from ninja import NinjaAPI
from sessions.api.views import sessions_router

from accounts.api.views import nav_router
from automation.api.views import router as automation_router
from chat.api.views import chat_router
from codebase.api.router import router as codebase_router

from . import __version__

# CSRF is enforced per-route at the auth-class level, not via a NinjaAPI flag:
# django-ninja 1.x marks every view csrf_exempt at the Django-middleware layer
# and delegates CSRF checking to the cookie/session auth classes. ``django_auth``
# (ninja.security.SessionAuth, the default csrf=True) runs ``check_csrf`` on
# every request it handles, so any route using ``django_auth`` (e.g. the chat /
# sessions / accounts routers) rejects cookie-authenticated unsafe methods
# without a matching X-CSRFToken. Bearer / API-key clients authenticate via
# HttpBearer / APIKeyHeader and never touch CSRF. ``tests/unit_tests/chat/api/
# test_csrf.py`` pins both halves on POST /api/chat/cancel.
api = NinjaAPI(version=__version__, title="Daiv API", docs_url="/docs/", urls_namespace="api")
api.add_router("/automation", automation_router)
api.add_router("/codebase", codebase_router)
api.add_router("/chat", chat_router)
api.add_router("/jobs", jobs_router)
api.add_router("/nav", nav_router)
api.add_router("/oauth", oauth_router)
api.add_router("/sessions", sessions_router)
