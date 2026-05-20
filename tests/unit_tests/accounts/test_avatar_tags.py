from types import SimpleNamespace

from django.template import Context, Template

from accounts.templatetags.avatar_tags import PALETTE_SIZE, user_color_index, user_initials


def _user(*, name="", email="", username=""):
    return SimpleNamespace(name=name, email=email, username=username)


def _render(template_source: str, context_dict: dict) -> str:
    return Template(template_source).render(Context(context_dict))


class TestUserInitials:
    def test_two_word_name_uses_first_and_last_letter(self):
        assert user_initials(_user(name="Ada Lovelace")) == "AL"

    def test_three_word_name_skips_middle(self):
        assert user_initials(_user(name="John Quincy Adams")) == "JA"

    def test_single_word_name_uses_first_two_letters(self):
        assert user_initials(_user(name="Madonna")) == "MA"

    def test_single_letter_name_returns_one_letter(self):
        assert user_initials(_user(name="X")) == "X"

    def test_falls_back_to_email_local_part_when_no_name(self):
        assert user_initials(_user(email="alice@example.com")) == "AL"

    def test_falls_back_to_username_when_no_name_or_email(self):
        # Keeps parity with ``user_color_index`` which also accepts username as
        # a final identifier — otherwise the chip would show "?" on a hashed
        # color, which reads as nonsense.
        assert user_initials(_user(username="zelda")) == "ZE"

    def test_lowercased_input_is_uppercased(self):
        assert user_initials(_user(name="bob smith")) == "BS"

    def test_returns_question_mark_when_user_is_none(self):
        assert user_initials(None) == "?"

    def test_returns_question_mark_when_all_fields_empty(self):
        assert user_initials(_user()) == "?"


class TestUserColorIndex:
    def test_index_in_valid_range(self):
        for u in [_user(username=f"user{i}", email=f"u{i}@x.com") for i in range(50)]:
            idx = user_color_index(u)
            assert 0 <= idx < PALETTE_SIZE

    def test_same_user_yields_same_index(self):
        u = _user(username="alice", email="alice@example.com")
        assert user_color_index(u) == user_color_index(u)

    def test_known_usernames_yield_known_indices(self):
        # Pinned golden values from the current md5(username) → bucket mapping.
        # If the hash, key choice, or PALETTE_SIZE changes, this test fails
        # loudly instead of silently reshuffling everyone's avatar color.
        expected = {"alice": 9, "andrew": 7, "amelia": 3, "aaron": 8, "anya": 0}
        for username, want in expected.items():
            assert user_color_index(_user(username=username)) == want

    def test_returns_zero_when_user_is_none(self):
        assert user_color_index(None) == 0

    def test_returns_zero_when_all_keys_empty(self):
        assert user_color_index(_user()) == 0

    def test_falls_back_through_username_email_name(self):
        only_username = user_color_index(_user(username="zelda"))
        only_email = user_color_index(_user(email="zelda@example.com"))
        assert 0 <= only_username < PALETTE_SIZE
        assert 0 <= only_email < PALETTE_SIZE


class TestTemplateIntegration:
    def test_user_initials_tag_renders(self):
        out = _render("{% load avatar_tags %}{% user_initials u %}", {"u": _user(name="Sandro Rodrigues")})
        assert out.strip() == "SR"

    def test_user_color_index_tag_renders_digit(self):
        out = _render("{% load avatar_tags %}{% user_color_index u %}", {"u": _user(username="sandro")})
        assert out.strip().isdigit()
        assert 0 <= int(out.strip()) < PALETTE_SIZE
