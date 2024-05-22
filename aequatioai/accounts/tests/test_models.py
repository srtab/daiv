from unittest.mock import Mock

from django.test import SimpleTestCase

from accounts.models import User, UserGroup


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

    def test_is_provider(self):
        user = Mock(spec=User, username="username", email="email")
        user.groups.values_list.return_value = [UserGroup.CLINICAL_PROVIDER]
        self.assertTrue(user.is_provider)

    def test_is_trueclinic(self):
        user = Mock(spec=User, username="username", email="email")
        user.groups.values_list.return_value = [UserGroup.TRUECLINIC]
        self.assertTrue(user.is_trueclinic)
