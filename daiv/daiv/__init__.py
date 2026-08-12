import os

__version__ = "2.0.0"

GIT_SHA = os.environ.get("DAIV_GIT_SHA", "")
GIT_SHA_SHORT = GIT_SHA[:7]
BUILD_DATE = os.environ.get("DAIV_BUILD_DATE", "")
REPO_URL = os.environ.get("DAIV_REPO_URL") or "https://github.com/srtab/daiv"
RELEASE = f"{__version__}+{GIT_SHA_SHORT}" if GIT_SHA else __version__

USER_AGENT = f"python-daiv-agent/{__version__}"
