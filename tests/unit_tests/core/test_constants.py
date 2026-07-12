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
