from pathlib import Path

from decouple import config

BASE_DIR = Path(__file__).absolute()

PROJECT_DIR = BASE_DIR.parent.parent.parent.parent

HOME_DIR = Path.home()
DATA_DIR = HOME_DIR / "data"

VERSION = config("VERSION", default=None)
BRANCH = config("BRANCH", default=None)
RELEASE = f"{BRANCH}:{VERSION}" if VERSION and BRANCH else None
ENVIRONMENT = config("ENVIRONMENT", default=None)
