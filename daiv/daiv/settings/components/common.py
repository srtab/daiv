from decouple import Csv, config
from get_docker_secret import get_docker_secret

DEBUG = config("DJANGO_DEBUG", default=False, cast=bool)

SECRET_KEY = get_docker_secret("DJANGO_SECRET_KEY", safe=False)
ALLOWED_HOSTS = config("DJANGO_ALLOWED_HOSTS", default="*", cast=Csv())
SITE_ID = 1

# Application definition

LOCAL_APPS = ["accounts", "automation", "codebase", "core"]

THIRD_PARTY_APPS = ["django_extensions"]

DJANGO_APPS = ["django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions"]

# See: https://docs.djangoproject.com/en/dev/ref/settings/#installed-apps
INSTALLED_APPS = LOCAL_APPS + THIRD_PARTY_APPS + DJANGO_APPS


MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
]


# TEMPLATE CONFIGURATION - https://docs.djangoproject.com/en/dev/ref/settings/#templates

TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True}]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

ROOT_URLCONF = "daiv.urls"

WSGI_APPLICATION = "daiv.wsgi.application"


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
