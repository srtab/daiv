from ninja import NinjaAPI

from codebase.api.views import router

api = NinjaAPI()
api.add_router("/codebase", router)
