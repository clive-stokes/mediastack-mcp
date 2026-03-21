"""Two-step confirmation protocol for write operations.

Every write operation returns a preview and a confirmation_id.
The agent must call mediastack_confirm(confirmation_id) to execute.
Unconfirmed actions expire after 5 minutes.
"""

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

EXPIRY_SECONDS = 300  # 5 minutes


@dataclass
class PendingAction:
    """A previewed but not-yet-confirmed write action."""
    confirmation_id: str
    description: str
    preview: dict
    execute_fn: Callable[[], Awaitable[dict]]
    created_at: float = field(default_factory=time.time)
    expiry_seconds: int = EXPIRY_SECONDS

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > self.expiry_seconds


class ConfirmationStore:
    """Thread-safe store for pending write actions."""

    def __init__(self):
        self._pending: dict[str, PendingAction] = {}
        self._lock = threading.Lock()

    def create(self, description: str, preview: dict,
               execute_fn: Callable[[], Awaitable[dict]],
               expiry_seconds: int = EXPIRY_SECONDS) -> PendingAction:
        """Register a pending action and return it with a confirmation_id."""
        self._cleanup()
        cid = secrets.token_hex(8)
        action = PendingAction(
            confirmation_id=cid,
            description=description,
            preview=preview,
            execute_fn=execute_fn,
            expiry_seconds=expiry_seconds,
        )
        with self._lock:
            self._pending[cid] = action
        return action

    def get(self, confirmation_id: str) -> PendingAction | None:
        """Retrieve and remove a pending action. Returns None if expired or not found."""
        self._cleanup()
        with self._lock:
            action = self._pending.pop(confirmation_id, None)
        if action and action.expired:
            return None
        return action

    def _cleanup(self) -> None:
        """Remove expired entries."""
        with self._lock:
            expired = [k for k, v in self._pending.items() if v.expired]
            for k in expired:
                del self._pending[k]


# Singleton
store = ConfirmationStore()
