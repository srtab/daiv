from jobs.api.views import jobs_router
from ninja import NinjaAPI

from chat.api.views import chat_router, models_router
from codebase.api.router import router as codebase_router

from . import __version__

api = NinjaAPI(version=__version__, title="Daiv API", docs_url="/docs/", urls_namespace="api")
api.add_router("/codebase", codebase_router)
api.add_router("/chat", chat_router)
api.add_router("/models", models_router)
api.add_router("/jobs", jobs_router)
