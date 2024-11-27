from chat.api.views import router as chat_router
from ninja import NinjaAPI

from codebase.api.views import router as codebase_router

api = NinjaAPI()
api.add_router("/codebase", codebase_router)
api.add_router("/v1", chat_router)
