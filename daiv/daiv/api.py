from chat.api.views import router as chat_router
from ninja import NinjaAPI

from codebase.api.views import router as codebase_router

from . import __version__

api = NinjaAPI(version=__version__, title="Daiv API")
api.add_router("/codebase", codebase_router)
api.add_router("/v1", chat_router)  # need to be start as /v1 to be compatible with OpenAI API
