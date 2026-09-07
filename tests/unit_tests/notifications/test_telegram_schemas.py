from __future__ import annotations

import json

import pytest
from notifications.telegram.commands import BaseCommand, get_command, register_command
from notifications.telegram.schemas import TGChat, display_handle, is_private_chat, parse_command, parse_update


@pytest.fixture(autouse=True)
def _preserve_command_registry():
    """Snapshot and restore the command registry around each test."""
    from notifications.telegram.commands import _registry

    snapshot = dict(_registry)
    yield
    _registry.clear()
    _registry.update(snapshot)


class TestParseUpdate:
    def test_parses_a_private_message(self):
        raw = json.dumps({
            "update_id": 1,
            "message": {
                "message_id": 9,
                "chat": {"id": 555, "type": "private", "username": "alice"},
                "text": "/start tok",
            },
        }).encode()
        update = parse_update(raw)
        assert update is not None
        assert update.message.chat.id == 555
        assert update.message.text == "/start tok"

    def test_parses_a_my_chat_member_block(self):
        raw = json.dumps({
            "update_id": 2,
            "my_chat_member": {"chat": {"id": 555, "type": "private"}, "new_chat_member": {"status": "kicked"}},
        }).encode()
        update = parse_update(raw)
        assert update is not None
        assert update.my_chat_member.new_chat_member.status == "kicked"

    def test_unmodelled_fields_are_ignored_not_rejected(self):
        # An update kind DAIV does not model must parse, so the route can answer 204 instead of
        # letting django-ninja emit the 422 that makes Telegram disable the webhook.
        raw = json.dumps({"update_id": 3, "poll_answer": {"poll_id": "p", "option_ids": [0]}}).encode()
        update = parse_update(raw)
        assert update is not None
        assert update.message is None
        assert update.my_chat_member is None

    def test_a_message_with_no_text_parses(self):
        raw = json.dumps({"update_id": 4, "message": {"chat": {"id": 5, "type": "private"}, "sticker": {}}}).encode()
        assert parse_update(raw).message.text is None

    @pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b'{"message": 3}'])
    def test_unparseable_bodies_return_none(self, raw):
        assert parse_update(raw) is None


class TestParseCommand:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("/start", ("start", "")),
            ("/start abc123", ("start", "abc123")),
            ("/START abc123", ("start", "abc123")),
            ("  /stop  ", ("stop", "")),
            ("/start@daiv_bot abc123", ("start", "abc123")),
            ("/start   abc123  ", ("start", "abc123")),
        ],
    )
    def test_recognised_forms(self, text, expected):
        assert parse_command(text) == expected

    @pytest.mark.parametrize("text", [None, "", "hello", "not /a command", "/", "//start"])
    def test_non_commands(self, text):
        assert parse_command(text) is None


class TestChatHelpers:
    def test_private_chat_predicate(self):
        assert is_private_chat(TGChat(id=1, type="private")) is True
        assert is_private_chat(TGChat(id=-1001, type="supergroup")) is False
        assert is_private_chat(None) is False

    @pytest.mark.parametrize(
        ("chat", "expected"),
        [
            (TGChat(id=1, username="alice", first_name="Alice"), "alice"),
            (TGChat(id=1, first_name="Alice"), "Alice"),
            (TGChat(id=1), ""),
            (None, ""),
        ],
    )
    def test_display_handle_prefers_the_username(self, chat, expected):
        assert display_handle(chat) == expected


class TestCommandRegistry:
    def test_registration_and_lookup(self):
        @register_command
        class PingCommand(BaseCommand):
            name = "test_ping"

            def handle(self, chat, argument):
                return f"pong:{argument}"

        command = get_command("test_ping")
        assert isinstance(command, PingCommand)
        assert command.handle(TGChat(id=1, type="private"), "x") == "pong:x"

    def test_unknown_command_returns_none(self):
        assert get_command("no_such_command") is None

    def test_a_concrete_command_without_a_name_fails_at_import_time(self):
        with pytest.raises(TypeError, match="must define `name`"):

            class Nameless(BaseCommand):
                def handle(self, chat, argument):
                    return None

    def test_duplicate_registration_is_rejected(self):
        @register_command
        class OnceCommand(BaseCommand):
            name = "test_once"

            def handle(self, chat, argument):
                return None

        with pytest.raises(ValueError, match="already registered"):

            @register_command
            class TwiceCommand(BaseCommand):
                name = "test_once"

                def handle(self, chat, argument):
                    return None
