"""Unit tests for the in-memory PortraitStore (Phase 4 — RED first).

The store is the media counterpart of the shared interview state driver: raw
portrait bytes, keyed by ``chat_id`` (int), loud on any wrong type or corrupt
value, idempotent and chat-scoped on delete.
"""
import pytest

from src.portrait_store import PortraitStore, PortraitStoreError


def _store() -> PortraitStore:
    return PortraitStore()


# --- happy path ----------------------------------------------------------------


def test_save_then_get_round_trips_exact_bytes():
    store = _store()
    payload = b"\xff\xd8\xff\xe0-portrait-bytes"

    store.save(101, payload)

    assert store.get(101) == payload


def test_get_unknown_chat_returns_none():
    store = _store()

    assert store.get(999) is None


def test_two_chats_are_isolated():
    store = _store()

    store.save(201, b"alpha")
    store.save(202, b"beta")

    assert store.get(201) == b"alpha"
    assert store.get(202) == b"beta"


def test_save_overwrites_a_previous_portrait_for_the_same_chat():
    store = _store()

    store.save(301, b"first")
    store.save(301, b"second")

    assert store.get(301) == b"second"


def test_empty_bytes_are_a_valid_portrait_payload():
    """The store does not judge content; it stores the exact bytes it is given."""
    store = _store()

    store.save(302, b"")

    assert store.get(302) == b""


# --- delete semantics ----------------------------------------------------------


def test_delete_removes_only_that_chats_portrait():
    store = _store()
    store.save(401, b"alpha")
    store.save(402, b"beta")

    store.delete(401)

    assert store.get(401) is None
    assert store.get(402) == b"beta"


def test_delete_is_idempotent():
    store = _store()
    store.save(501, b"alpha")

    store.delete(501)
    store.delete(501)  # second call: nothing there, must not raise

    assert store.get(501) is None


def test_delete_unknown_chat_is_a_noop():
    store = _store()

    store.delete(599)

    assert store.get(599) is None


# --- wrong-typed chat_id → loud -------------------------------------------------


@pytest.mark.parametrize("bad_chat_id", ["1", "abc", 1.0, None, True, False])
def test_wrong_typed_chat_id_raises_on_save(bad_chat_id):
    store = _store()

    with pytest.raises(PortraitStoreError, match="chat_id must be int"):
        store.save(bad_chat_id, b"bytes")


@pytest.mark.parametrize("bad_chat_id", ["1", 1.5, None, True])
def test_wrong_typed_chat_id_raises_on_get(bad_chat_id):
    store = _store()

    with pytest.raises(PortraitStoreError, match="chat_id must be int"):
        store.get(bad_chat_id)


@pytest.mark.parametrize("bad_chat_id", ["1", 1.5, None, False])
def test_wrong_typed_chat_id_raises_on_delete(bad_chat_id):
    store = _store()

    with pytest.raises(PortraitStoreError, match="chat_id must be int"):
        store.delete(bad_chat_id)


# --- wrong-typed / corrupt values → loud ---------------------------------------


def test_save_rejects_non_bytes_payload_loudly():
    store = _store()

    with pytest.raises(PortraitStoreError, match="bytes"):
        store.save(601, "not bytes")  # type: ignore[arg-type]


def test_get_raises_on_corrupt_stored_value():
    """A foreign value smuggled into the store must never be returned silently."""
    store = _store()
    store._portraits[701] = "corrupt string"  # type: ignore[assignment]

    with pytest.raises(PortraitStoreError, match="corrupt"):
        store.get(701)


def test_delete_still_works_and_purges_a_corrupt_value():
    store = _store()
    store._portraits[702] = object()  # type: ignore[assignment]

    store.delete(702)

    assert store.get(702) is None
