"""Two-step confirmation protocol for write operations.

Every write operation returns a preview and a confirmation_id.
The agent must call mediastack_confirm(confirmation_id) to execute.
Unconfirmed actions expire after 5 minutes (2 minutes for file deletion).

If a confirmation has expired, calling confirm with the old ID will
re-issue the preview with a fresh confirmation_id rather than failing.
"""

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

EXPIRY_SECONDS = 300  # 5 minutes
# Expired actions kept for this long to allow re-issue on confirm attempt.
EXPIRED_RETENTION_SECONDS = 3600  # 1 hour


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

    def renew(self) -> "PendingAction":
        """Create a fresh copy with a new ID and reset expiry."""
        return PendingAction(
            confirmation_id=secrets.token_hex(8),
            description=self.description,
            preview=self.preview,
            execute_fn=self.execute_fn,
            expiry_seconds=self.expiry_seconds,
        )


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
        """Retrieve and remove a live (non-expired) action. Returns None if expired or not found."""
        self._cleanup()
        with self._lock:
            action = self._pending.get(confirmation_id)
            if action and not action.expired:
                del self._pending[confirmation_id]
                return action
        return None

    def get_expired(self, confirmation_id: str) -> PendingAction | None:
        """Retrieve an expired action for re-issue. Returns None if not found or still live."""
        with self._lock:
            action = self._pending.get(confirmation_id)
            if action and action.expired:
                del self._pending[confirmation_id]
                return action
        return None

    def renew(self, expired_action: PendingAction) -> PendingAction:
        """Re-issue an expired action with a fresh ID and expiry."""
        renewed = expired_action.renew()
        with self._lock:
            self._pending[renewed.confirmation_id] = renewed
        logger.info("Renewed expired confirmation %s -> %s: %s",
                     expired_action.confirmation_id, renewed.confirmation_id,
                     renewed.description)
        return renewed

    def _cleanup(self) -> None:
        """Remove actions that are well past expiry (retention window)."""
        now = time.time()
        with self._lock:
            stale = [
                k for k, v in self._pending.items()
                if (now - v.created_at) > (v.expiry_seconds + EXPIRED_RETENTION_SECONDS)
            ]
            for k in stale:
                del self._pending[k]


# Singleton
store = ConfirmationStore()
