from unittest.mock import patch

from core import worker_liveness


def test_a_worker_is_alive_once_it_refreshed_its_key():
    worker_liveness.refresh("w-alive")

    assert worker_liveness.alive("w-alive") is True
    assert worker_liveness.alive("w-never-seen") is False


def test_a_failed_read_counts_as_alive(caplog):
    with patch.object(worker_liveness.cache, "get", side_effect=ConnectionError("redis down")):
        assert worker_liveness.alive("w") is True

    assert "Could not read whether worker_id=w is alive" in caplog.text
