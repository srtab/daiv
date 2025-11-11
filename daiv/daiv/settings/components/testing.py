TESTING = True

# I18N

LANGUAGES = (("en", "English"),)
LANGUAGE_CODE = "en"

# WAGTAIL

WAGTAIL_CONTENT_LANGUAGES = LANGUAGES
WAGTAILADMIN_PERMITTED_LANGUAGES = LANGUAGES

# Use memory database to run tests faster

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}


# Use memory cache to run tests faster

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


# Use simpler hashes to run tests faster

PASSWORD_HASHERS = ("django.contrib.auth.hashers.MD5PasswordHasher",)


# ignore emails

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


# CELERY

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory:///"


# LOGGING

LOGGING = {
    "version": 1,
    "disable_existing_loggers": True,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "loggers": {"": {"level": "NOTSET", "handlers": ["null"]}},
}
