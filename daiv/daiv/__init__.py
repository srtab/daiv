import os

__version__ = "2.0.0"

GIT_SHA = os.environ.get("DAIV_GIT_SHA", "")
BUILD_DATE = os.environ.get("DAIV_BUILD_DATE", "")
RELEASE = f"{__version__}+{GIT_SHA[:7]}" if GIT_SHA else __version__

USER_AGENT = f"python-daiv-agent/{__version__}"
