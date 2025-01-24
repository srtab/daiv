from ninja import NinjaAPI

from chat.api.views import chat_router, models_router
from codebase.api.views import router as codebase_router

from . import __version__

api = NinjaAPI(version=__version__, title="Daiv API", docs_url="/docs/")
api.add_router("/codebase", codebase_router)
api.add_router("/chat", chat_router)
api.add_router("/models", models_router)
