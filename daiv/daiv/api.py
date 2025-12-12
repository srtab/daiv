from functools import wraps

from ninja import NinjaAPI

from automation.api.views import router as automation_router
from chat.api.views import chat_router, models_router
from codebase.api.router import router as codebase_router

from . import __version__


def cors_headers(func):
    @wraps(func)
    async def wrapper(request, *args, **kwargs):
        response = await func(request, *args, **kwargs)
        response["Access-Control-Allow-Origin"] = "*"
        response["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response["Access-Control-Allow-Credentials"] = "true"
        response["Access-Control-Max-Age"] = "86400"
        return response

    return wrapper


api = NinjaAPI(version=__version__, title="Daiv API", docs_url="/docs/")
api.add_decorator(cors_headers, mode="view")
api.add_router("/automation", automation_router)
api.add_router("/codebase", codebase_router)
api.add_router("/chat", chat_router)
api.add_router("/models", models_router)
