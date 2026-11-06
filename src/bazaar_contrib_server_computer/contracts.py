"""What other modules hold server-computer to, and what a relay between the
server's replicas holds to."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
lazy from collections.abc import Sequence

from bazaar_core import EventKey, ServiceKey
lazy from bazaar_contrib_audit import AuditEvent
lazy from bazaar_contrib_sidecar.wire import Failure, Request, Result
lazy from pydantic import JsonValue

# how long a computer's listen is held when nothing is waiting for it
HOLD_MS = 25_000
# how many calls one listen hands over at most
CALLS_AT_MOST = 20
# how long a call waits for its answer before the computer counts as silent
ANSWER_MS = 60_000


@dataclass(frozen=True, slots=True)
class Call:
    """A call waiting for its computer: its number there, and what it asks."""

    number: int
    request: Request


class Relay(Protocol):
    """What the server's replicas share about computers: the calls waiting
    for them, the answers coming back, and when each was last here. A
    computer listens on any replica and a page calls it from any other; the
    relay keeps only what lives while it does."""

    async def ask(self, computer_id: str, request: Request) -> Result | Failure:
        """Hand the call to the computer and wait for its answer, for
        ANSWER_MS at most; the same call still waiting is joined rather than
        handed over again. TimeoutError when no answer comes."""
        ...

    async def calls(self, computer_id: str, after: int) -> Sequence[Call]:
        """The calls for the computer numbered after the given one, oldest
        first and CALLS_AT_MOST of them at most; when there are none, the
        first to come within HOLD_MS, or nothing."""
        ...

    async def answer(
        self, computer_id: str, number: int, response: Result | Failure
    ) -> None:
        """The computer's answer to its call; one given again counts once."""
        ...

    async def seen(self, computer_id: str) -> int | None:
        """Note that the computer is here now; when it was here before,
        nothing when not since the relay started."""
        ...

    async def last_seen(self, computer_ids: Sequence[str]) -> dict[str, int]:
        """When each of the computers was last here, those seen since the
        relay started."""
        ...


RELAY = ServiceKey[Relay]("server.relay")


class Computers(Protocol):
    """What other modules do with the computers."""

    async def call(
        self,
        computer_id: str,
        service: str,
        method: str,
        *args: object,
        **kwargs: object,
    ) -> JsonValue:
        """Call a member the computer exposes, as sidecar's Client does:
        RemoteError when it fails there, TimeoutError when the computer does
        not answer in time."""
        ...


COMPUTERS = ServiceKey[Computers]("server.computers")


@dataclass(frozen=True, slots=True)
class Reported:
    """An audit event a computer reported, announced once however often the
    computer sends it again."""

    computer_id: str
    event: AuditEvent


REPORTED = EventKey[Reported]("server.computer.reported")
