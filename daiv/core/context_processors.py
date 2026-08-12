from datetime import datetime

from daiv import BUILD_DATE, GIT_SHA, __version__

COMMIT_URL_TEMPLATE = "https://github.com/srtab/daiv/commit/{sha}"


def build_info(request):
    build_date = None
    if BUILD_DATE:
        try:
            build_date = datetime.fromisoformat(BUILD_DATE)
        except ValueError:
            build_date = None
    return {
        "build_info": {
            "version": __version__,
            "sha": GIT_SHA or None,
            "sha_short": GIT_SHA[:7] if GIT_SHA else None,
            "build_date": build_date,
            "commit_url": COMMIT_URL_TEMPLATE.format(sha=GIT_SHA) if GIT_SHA else None,
        }
    }
