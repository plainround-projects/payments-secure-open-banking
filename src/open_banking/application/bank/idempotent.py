"""The idempotent executor of the bank: one transaction holds the key, the effect and the stored response together."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from open_banking.application.bank.ports import Fresh, Replay, UnitOfWork, UnitOfWorkFactory
from open_banking.application.logs import fields
from open_banking.application.ports import Clock
from open_banking.domain.errors import IdempotencyKeyReusedError
from open_banking.domain.idempotency import IdempotencyKey, StoredResponse

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Execution:
    """The response to send, and whether it came from the store instead of fresh work."""

    response: StoredResponse
    replayed: bool


class IdempotentExecutor:
    """Runs a write at most once per (client, key), however many times and however concurrently it is sent."""

    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock, key_time_to_live: timedelta) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._time_to_live = key_time_to_live

    def execute(
        self,
        *,
        client_id: str,
        key: IdempotencyKey,
        request_fingerprint: str,
        work: Callable[[UnitOfWork], StoredResponse],
    ) -> Execution:
        """Claim the key, run `work` if it is new, store the response, commit. An exception rolls everything back."""
        with self._uow_factory() as uow:
            now = self._clock.now()
            claim = uow.keys.claim(client_id, key, request_fingerprint, now)
            if isinstance(claim, Replay):
                logger.info("replayed", extra=fields(client_id=client_id, idempotency_key=key.value))
                return Execution(claim.response, replayed=True)
            if not isinstance(claim, Fresh):
                raise IdempotencyKeyReusedError(
                    f"the Idempotency-Key {key.value!r} was already used for a different request"
                )
            response = work(uow)
            uow.keys.store(client_id, key, request_fingerprint, response, now, now + self._time_to_live)
            return Execution(response, replayed=False)
