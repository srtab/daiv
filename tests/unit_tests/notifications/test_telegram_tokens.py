from __future__ import annotations

import re
import time

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

    def test_a_tampered_mac_is_rejected(self):
        token = mint_token(42, **NO_BINDING)
        # The final character encodes only 2 significant bits, so substituting it can decode to
        # the identical byte; the second-to-last carries a full 6.
        tampered = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
        assert verify_token(tampered, **NO_BINDING) is None
