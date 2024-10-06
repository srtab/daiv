from django.test import SimpleTestCase

from accounts.models import User


class UserModelTest(SimpleTestCase):
    def test_str(self):
        user = User(email="email")
        self.assertEqual(str(user), user.email)

    def test_str_with_name_defined(self):
        user = User(name="name", email="email")
        self.assertEqual(str(user), user.name)

    def test_str_with_username_defined(self):
        user = User(username="username", email="email")
        self.assertEqual(str(user), user.username)
