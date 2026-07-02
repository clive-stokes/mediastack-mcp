"""Tier 1 — confirmation protocol logic. No I/O."""

import threading

from app.confirmations import (
    ConfirmationStore,
    EXPIRED_RETENTION_SECONDS,
    EXPIRY_SECONDS,
)


class RecordingFn:
    """Async-callable fake that records invocations."""

    def __init__(self):
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        return {"status": "done"}


def make_action(store, expiry_seconds=EXPIRY_SECONDS):
    fn = RecordingFn()
    action = store.create(
        "Test action", {"action": "delete", "title": "X"}, fn,
        expiry_seconds=expiry_seconds,
    )
    return action, fn


def expire(action, seconds=1):
    """Push an action past its expiry window."""
    action.created_at -= action.expiry_seconds + seconds


def test_confirm_executes_exactly_once():
    store = ConfirmationStore()
    action, _ = make_action(store)

    first = store.get(action.confirmation_id)
    assert first is action

    # Second confirm with the same ID: not found
    assert store.get(action.confirmation_id) is None
    assert store.get_expired(action.confirmation_id) is None


def test_expired_action_get_returns_none_but_get_expired_returns_it():
    store = ConfirmationStore()
    action, _ = make_action(store)
    expire(action)

    assert store.get(action.confirmation_id) is None
    recovered = store.get_expired(action.confirmation_id)
    assert recovered is action
    # get_expired consumes it
    assert store.get_expired(action.confirmation_id) is None


def test_get_expired_does_not_return_live_action():
    store = ConfirmationStore()
    action, _ = make_action(store)
    assert store.get_expired(action.confirmation_id) is None
    # Still retrievable live
    assert store.get(action.confirmation_id) is action


def test_renewal_issues_fresh_id_with_reset_expiry_and_same_execute_fn():
    store = ConfirmationStore()
    action, fn = make_action(store, expiry_seconds=120)
    expire(action)

    expired = store.get_expired(action.confirmation_id)
    renewed = store.renew(expired)

    assert renewed.confirmation_id != action.confirmation_id
    assert renewed.execute_fn is fn
    assert renewed.expiry_seconds == 120
    assert not renewed.expired
    assert renewed.description == action.description
    assert renewed.preview == action.preview


def test_expired_destructive_action_cannot_execute_without_second_confirm():
    """The property the protocol exists to guarantee: an expired file-deletion
    preview never executes off the back of the old confirmation ID."""
    store = ConfirmationStore()
    action, fn = make_action(store, expiry_seconds=120)
    expire(action)

    # The confirm tool's flow: live lookup fails ...
    assert store.get(action.confirmation_id) is None
    # ... so it renews instead of executing.
    renewed = store.renew(store.get_expired(action.confirmation_id))
    assert fn.calls == 0

    # Old ID is dead even after renewal
    assert store.get(action.confirmation_id) is None

    # Only an explicit confirm of the NEW id executes
    live = store.get(renewed.confirmation_id)
    assert live is renewed
    assert fn.calls == 0  # retrieval still isn't execution


def test_cleanup_purges_past_retention_window():
    store = ConfirmationStore()
    action, _ = make_action(store)
    # Push past expiry + retention
    action.created_at -= action.expiry_seconds + EXPIRED_RETENTION_SECONDS + 1

    # create() triggers _cleanup
    make_action(store)

    assert store.get(action.confirmation_id) is None
    assert store.get_expired(action.confirmation_id) is None


def test_concurrent_create_and_get_leaves_store_consistent():
    store = ConfirmationStore()
    ids: list[str] = []
    lock = threading.Lock()

    def worker():
        for _ in range(50):
            action, _ = make_action(store)
            with lock:
                ids.append(action.confirmation_id)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ids) == len(set(ids)) == 400

    # Every action retrievable exactly once across racing consumers
    results: list = []

    def consumer(chunk):
        got = [store.get(cid) for cid in chunk]
        with lock:
            results.extend(g for g in got if g)

    consumers = [
        threading.Thread(target=consumer, args=(ids,)) for _ in range(4)
    ]
    for t in consumers:
        t.start()
    for t in consumers:
        t.join()

    assert len(results) == 400
    assert len(store._pending) == 0

def test_reinstate_after_failed_execute_allows_retry():
    store = ConfirmationStore()
    action, fn = make_action(store)

    taken = store.get(action.confirmation_id)
    assert taken is action
    # Simulate execute_fn failing: the caller reinstates the action so the
    # same confirmation_id can be retried.
    store.reinstate(taken)

    retried = store.get(action.confirmation_id)
    assert retried is action

    # A reinstated action keeps its original created_at — expiry still applies.
    expire(action)
    store.reinstate(action)
    assert store.get(action.confirmation_id) is None
    assert store.get_expired(action.confirmation_id) is action
