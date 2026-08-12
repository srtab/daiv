import contextlib
from datetime import datetime

from daiv import BUILD_DATE, GIT_SHA, GIT_SHA_SHORT, REPO_URL, __version__


def _compute_build_info() -> dict:
    build_date = None
    if BUILD_DATE:
        with contextlib.suppress(ValueError):
            build_date = datetime.fromisoformat(BUILD_DATE)
    return {
        "version": __version__,
        "sha": GIT_SHA or None,
        "sha_short": GIT_SHA_SHORT or None,
        "build_date": build_date,
        "commit_url": f"{REPO_URL}/commit/{GIT_SHA}" if GIT_SHA else None,
    }


_BUILD_INFO = _compute_build_info()


def build_info(request):
    return {"build_info": _BUILD_INFO}
