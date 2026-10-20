from codebase.conf import settings


def git_platform(request):
    return {"git_platform": settings.CLIENT.value}
