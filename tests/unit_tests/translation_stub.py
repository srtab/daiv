"""A fake locale for asserting template strings reach the page translated.

A stub rather than a real locale because ``.mo`` files are gitignored and CI runs
``make test`` without ``compilemessages``, so no compiled catalog exists there.
"""

import contextlib
import gettext as gettext_module

from django.utils import translation
from django.utils.translation import trans_real


class _StubCatalog(gettext_module.NullTranslations):
    def __init__(self, entries):
        super().__init__()
        self._entries = entries

    def gettext(self, message):
        return self._entries.get(message, message)


@contextlib.contextmanager
def catalog(entries):
    """Activate a fake locale carrying ``entries``."""
    trans_real._translations["xx"] = _StubCatalog(entries)
    try:
        with translation.override("xx"):
            yield
    finally:
        trans_real._translations.pop("xx", None)
