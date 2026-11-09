"""COMPUTERS: calling what a computer exposes, through the relay to wherever
it listens, the computers as other modules see them, and what another
module shows in their stalls."""

from __future__ import annotations

from contextlib import asynccontextmanager
lazy from collections.abc import AsyncIterator, Sequence
lazy from time import time_ns

lazy from bazaar_contrib_sidecar import RemoteError
lazy from bazaar_contrib_sidecar.wire import Failure, Request
lazy from pydantic import JsonValue
lazy from pydantic_core import to_jsonable_python

lazy from .contracts import ComputerSummary, Relay
lazy from .starlink import is_online
lazy from .store import Store


class Calls:
    def __init__(self, relay: Relay, store: Store) -> None:
        self._relay = relay
        self._store = store
        # where the goods each stall shows under its name are, while registered
        self.goods: str | None = None

    async def call(
        self,
        computer_id: str,
        service: str,
        method: str,
        *args: object,
        **kwargs: object,
    ) -> JsonValue:
        response = await self._relay.ask(
            computer_id,
            Request(
                service=service,
                method=method,
                args=to_jsonable_python(args),
                kwargs=to_jsonable_python(kwargs),
            ),
        )
        if isinstance(response, Failure):
            raise RemoteError(response.type, response.message)
        return response.value

    async def computers(self, ids: Sequence[str] | None) -> Sequence[ComputerSummary]:
        names = await self._store.names(None if ids is None else frozenset(ids))
        seen = await self._relay.last_seen(list(names))
        now = time_ns() // 1_000_000
        return [
            ComputerSummary(
                id=computer_id,
                name=name,
                online=is_online(seen.get(computer_id), now),
                seen_ms=seen.get(computer_id),
            )
            for computer_id, name in sorted(names.items())
        ]

    @asynccontextmanager
    async def stall(self, src: str) -> AsyncIterator[None]:
        self.goods = src
        try:
            yield
        finally:
            self.goods = None
