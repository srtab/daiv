from pathlib import Path

from decouple import config

BASE_DIR = Path(__file__).absolute()

PROJECT_DIR = BASE_DIR.parent.parent.parent.parent

HOME_DIR = Path.home()
DATA_DIR = HOME_DIR / "data"

ENVIRONMENT = config("ENVIRONMENT", default=None)
