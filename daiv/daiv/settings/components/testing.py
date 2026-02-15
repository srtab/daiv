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

CACHES = {"default": {"BACKEND": "core.cache.LocMemCache"}}


# Use simpler hashes to run tests faster

PASSWORD_HASHERS = ("django.contrib.auth.hashers.MD5PasswordHasher",)


# ignore emails

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


# TASKS

TASKS = {"default": {"BACKEND": "core.backends.immediate.ImmediateBackend"}}


# LOGGING

LOGGING = {
    "version": 1,
    "disable_existing_loggers": True,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "loggers": {"": {"level": "NOTSET", "handlers": ["null"]}},
}
