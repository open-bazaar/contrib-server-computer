"""COMPUTERS: calling what a computer exposes, through the relay to wherever
it listens."""

from __future__ import annotations

lazy from bazaar_contrib_sidecar import RemoteError
lazy from bazaar_contrib_sidecar.wire import Failure, Request
lazy from pydantic import JsonValue
lazy from pydantic_core import to_jsonable_python

lazy from .contracts import Relay


class Calls:
    def __init__(self, relay: Relay) -> None:
        self._relay = relay

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
