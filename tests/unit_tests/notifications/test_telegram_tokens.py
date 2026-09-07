from __future__ import annotations

import re
import time

import pytest
from notifications.telegram.tokens import TOKEN_TTL_SECONDS, mint_token, peek_user_pk, verify_token

# The deep-link payload alphabet Telegram accepts. Violating it fails in production only.
_TG_START_PAYLOAD = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")

NO_BINDING = {"address": "", "verified_at": ""}


class TestWireFormat:
    def test_token_fits_the_telegram_deep_link_payload(self):
        # THE assertion that would otherwise only fail in production: 64 chars, base64url only.
        token = mint_token(2**62, address="123456789", verified_at="2026-08-21T10:00:00+00:00")
        assert _TG_START_PAYLOAD.match(token), token
        assert len(token) == 38

    def test_length_is_constant_across_pk_and_state_sizes(self):
        short = mint_token(1, **NO_BINDING)
        long = mint_token(9_007_199_254_740_991, address="-1001234567890", verified_at="x" * 200)
        assert len(short) == len(long) == 38


class TestRoundTrip:
    def test_verifies_with_the_state_it_was_minted_against(self):
        token = mint_token(42, **NO_BINDING)
        assert peek_user_pk(token) == 42
        assert verify_token(token, **NO_BINDING) == 42

    def test_verifies_with_a_real_binding_state(self):
        state = {"address": "555", "verified_at": "2026-08-21T10:00:00+00:00"}
        token = mint_token(42, **state)
        assert verify_token(token, **state) == 42


class TestExpiry:
    def test_expired_token_is_rejected_by_both_entry_points(self):
        now = int(time.time())
        token = mint_token(42, now=now - TOKEN_TTL_SECONDS - 1, **NO_BINDING)
        assert peek_user_pk(token) is None
        assert verify_token(token, **NO_BINDING) is None

    def test_token_one_second_inside_the_window_still_verifies(self):
        now = int(time.time())
        token = mint_token(42, now=now - TOKEN_TTL_SECONDS + 1, **NO_BINDING)
        assert verify_token(token, **NO_BINDING) == 42


class TestStateInvalidation:
    def test_a_pre_bind_token_dies_once_a_binding_exists(self):
        token = mint_token(42, **NO_BINDING)
        assert verify_token(token, address="555", verified_at="2026-08-21T10:00:00+00:00") is None

    def test_rebinding_the_same_chat_id_still_kills_earlier_tokens(self):
        # The address alone would NOT move on a reconnect-after-block: the delivery-time 403
        # flip leaves the row in place with the same chat_id. Folding verified_at is what
        # keeps pre-bind tokens from staying live for their full TTL on that path.
        token = mint_token(42, address="555", verified_at="2026-08-21T10:00:00+00:00")
        assert verify_token(token, address="555", verified_at="2026-08-21T11:00:00+00:00") is None

    def test_each_pk_gets_its_own_token(self):
        # The pk is inside the MAC input, so two users can never share a token.
        assert mint_token(42, **NO_BINDING) != mint_token(43, **NO_BINDING)
        assert verify_token(mint_token(42, **NO_BINDING), **NO_BINDING) == 42
        assert verify_token(mint_token(43, **NO_BINDING), **NO_BINDING) == 43


class TestMalformedInput:
    def test_garbage_is_rejected(self):
        for bad in ["", "not-a-token", "!!!!", "A" * 65, mint_token(42, **NO_BINDING)[:-4]]:
            assert peek_user_pk(bad) is None
            assert verify_token(bad, **NO_BINDING) is None

    @pytest.mark.parametrize("bad", ["A", "AAAAA", "A" * 37])
    def test_a_length_that_breaks_base64_padding_is_rejected_not_raised(self, bad):
        # The only inputs that make urlsafe_b64decode raise: length 1 mod 4. Everything else
        # (including "!!!!") decodes, because non-alphabet characters are discarded — so the
        # `except ValueError` guard is reachable only through these, and a user can type one
        # straight into the bot chat.
        assert len(bad) % 4 == 1
        assert peek_user_pk(bad) is None
        assert verify_token(bad, **NO_BINDING) is None

    def test_the_nul_separator_keeps_the_mac_fields_from_sliding(self):
        # Without it ("a", "bc") and ("ab", "c") hash identically, so a binding-state change
        # that only moved a boundary would not invalidate an outstanding token.
        token = mint_token(42, address="a", verified_at="bc")
        assert verify_token(token, address="ab", verified_at="c") is None
        assert verify_token(token, address="a", verified_at="bc") == 42

    @pytest.mark.parametrize("user_pk", [0, 2**63, 2**64 - 1])
    def test_an_out_of_range_pk_is_rejected_before_it_reaches_a_query(self, user_pk):
        # ``peek_user_pk`` feeds an unauthenticated pk straight into a binding lookup, and the
        # ``>Q`` codec would otherwise hand it a value SQLite answers with OverflowError.
        token = mint_token(user_pk, **NO_BINDING)
        assert peek_user_pk(token) is None
        assert verify_token(token, **NO_BINDING) is None

    def test_the_largest_legal_pk_still_round_trips(self):
        token = mint_token(2**63 - 1, **NO_BINDING)
        assert peek_user_pk(token) == 2**63 - 1
        assert verify_token(token, **NO_BINDING) == 2**63 - 1

    def test_a_tampered_mac_is_rejected(self):
        token = mint_token(42, **NO_BINDING)
        # The final character encodes only 2 significant bits, so substituting it can decode to
        # the identical byte; the second-to-last carries a full 6.
        tampered = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
        assert verify_token(tampered, **NO_BINDING) is None
