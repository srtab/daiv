import pytest

from core.constants import BOT_AUTO_LABEL, BOT_LABEL, BOT_MAX_LABEL, has_auto_label, has_bot_label, has_max_label


@pytest.mark.parametrize("label", [BOT_LABEL, BOT_AUTO_LABEL, BOT_MAX_LABEL, "DAIV-Max"])
def test_any_bot_label_counts_regardless_of_case(label):
    assert has_bot_label(["bug", label]) is True


@pytest.mark.parametrize("labels", [[], ["bug", "feature"], ["daiv: fix"]])
def test_labels_without_a_bot_label_do_not_count(labels):
    assert has_bot_label(labels) is False


def test_only_the_max_label_asks_for_max_mode():
    assert has_max_label([BOT_LABEL, "DAIV-MAX"]) is True
    assert has_max_label([BOT_LABEL, BOT_AUTO_LABEL]) is False


def test_only_the_auto_label_asks_to_skip_the_plan():
    assert has_auto_label([BOT_LABEL, "DAIV-AUTO"]) is True
    assert has_auto_label([BOT_LABEL, BOT_MAX_LABEL]) is False


def test_chat_terminal_messages_live_in_core_constants():
    from core.constants import CANCELLED_BY_USER_MESSAGE, INTERRUPTED_MESSAGE, RUN_FAILED_MESSAGE

    assert CANCELLED_BY_USER_MESSAGE == "Stopped by user."
    assert INTERRUPTED_MESSAGE == "Run was interrupted before completing."
    assert RUN_FAILED_MESSAGE.startswith("Run failed.")


def test_streaming_reexports_the_same_constant_objects():
    from chat.api import streaming
    from core import constants

    assert streaming.CANCELLED_BY_USER_MESSAGE is constants.CANCELLED_BY_USER_MESSAGE
    assert streaming.INTERRUPTED_MESSAGE is constants.INTERRUPTED_MESSAGE
    assert streaming.RUN_FAILED_MESSAGE is constants.RUN_FAILED_MESSAGE
