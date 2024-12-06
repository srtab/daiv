from decouple import Csv, config
from get_docker_secret import get_docker_secret

from daiv.settings.components import DATA_DIR, PROJECT_DIR, RELEASE

DEBUG = config("DJANGO_DEBUG", default=False, cast=bool)

SECRET_KEY = get_docker_secret("DJANGO_SECRET_KEY", safe=False)
ALLOWED_HOSTS = config("DJANGO_ALLOWED_HOSTS", default="*", cast=Csv())
SITE_ID = 1
RELEASE_VERSION = RELEASE

# Application definition

LOCAL_APPS = ["accounts", "codebase", "core"]

THIRD_PARTY_APPS = ["django_celery_beat", "django_extensions"]

DJANGO_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.humanize",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.contrib.sites",
    "django.contrib.staticfiles",
]

# See: https://docs.djangoproject.com/en/dev/ref/settings/#installed-apps
INSTALLED_APPS = LOCAL_APPS + THIRD_PARTY_APPS + DJANGO_APPS


MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "csp.middleware.CSPMiddleware",
]


# TEMPLATE CONFIGURATION - https://docs.djangoproject.com/en/dev/ref/settings/#templates

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [PROJECT_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.contrib.auth.context_processors.auth",
                "django.template.context_processors.request",
                "django.template.context_processors.media",
                "django.template.context_processors.static",
                "django.template.context_processors.csrf",
                "django.template.context_processors.tz",
                "django.template.context_processors.i18n",
                "django.template.context_processors.debug",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

ROOT_URLCONF = "daiv.urls"

WSGI_APPLICATION = "daiv.wsgi.application"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


# Password validation
# https://docs.djangoproject.com/en/1.9/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    # https://random-ize.com/how-long-to-hack-pass/
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 9}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# Auth settings

AUTH_USER_MODEL = "accounts.User"


# MESSAGES

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"


# SESSION

SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_NAME = "__Secure-sessionid"
SESSION_COOKIE_SECURE = True


# SECURITY - https://docs.djangoproject.com/en/dev/ref/middleware/#module-django.middleware.security

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = True
SECURE_HSTS_SECONDS = 31536000
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
# Use CSRF in session instead of cookie: https://docs.djangoproject.com/en/dev/ref/csrf/
CSRF_USE_SESSIONS = True
X_FRAME_OPTIONS = "DENY"


# Static files (CSS, JavaScript, Images)

STATIC_URL = "/static/"
STATIC_ROOT = DATA_DIR / "static"
STATICFILES_FINDERS = (
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
)


# Media files
MEDIA_ROOT = DATA_DIR / "media"
MEDIA_URL = "/media/"

FILE_UPLOAD_PERMISSIONS = 0o644


# Email settings

EMAIL_SUBJECT_PREFIX = "[DAIV]"
SERVER_EMAIL = DEFAULT_FROM_EMAIL = "DAIV <dev@daiv.dev>"

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = config("DJANGO_EMAIL_HOST", default="localhost")
EMAIL_HOST_USER = config("DJANGO_EMAIL_HOST_USER", default=None)
EMAIL_HOST_PASSWORD = get_docker_secret("DJANGO_EMAIL_HOST_PASSWORD")
EMAIL_PORT = config("DJANGO_EMAIL_PORT", default=25, cast=int)
EMAIL_USE_TLS = config("DJANGO_EMAIL_USE_TLS", default=False, cast=bool)
EMAIL_TIMEOUT = 15
