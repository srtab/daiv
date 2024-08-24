from django.test import SimpleTestCase

from accounts.adapter import AccountAdapter


class AccountAdapterTest(SimpleTestCase):
    def test_is_open_for_signup(self):
        self.assertFalse(AccountAdapter().is_open_for_signup(None))
