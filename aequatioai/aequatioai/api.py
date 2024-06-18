from ninja import NinjaAPI

from codebase.api import router

api = NinjaAPI()
api.add_router("/codebase", router)
