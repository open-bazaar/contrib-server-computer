"""What other modules hold server-computer to, and what a relay between the
server's replicas holds to."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
lazy from collections.abc import Sequence
lazy from contextlib import AbstractAsyncContextManager

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

    async def seen(self, computer_id: str) -> tuple[int | None, int]:
        """Note that the computer is here now: when it was here before,
        nothing when not since the relay started, and when it is now, as
        the relay noted it."""
        ...

    async def last_seen(self, computer_ids: Sequence[str]) -> dict[str, int]:
        """When each of the computers was last here, those seen since the
        relay started."""
        ...


RELAY = ServiceKey[Relay]("server.relay")


@dataclass(frozen=True, slots=True)
class ComputerSummary:
    """A computer as other modules show it: its name, whether it is here,
    and when it last was, nothing when not since the relay started."""

    id: str
    name: str
    online: bool
    seen_ms: int | None


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

    async def computers(self, ids: Sequence[str] | None) -> Sequence[ComputerSummary]:
        """The computers given that are there, every one for None."""
        ...

    def stall(self, src: str) -> AbstractAsyncContextManager[None]:
        """Show what is at <src>/<the computer's ref> in each computer's stall
        on the overview, under its name, while open; one at a time."""
        ...


COMPUTERS = ServiceKey[Computers]("server.computers")


@dataclass(frozen=True, slots=True)
class Reported:
    """An audit event a computer reported, announced once however often the
    computer sends it again."""

    computer_id: str
    event: AuditEvent


REPORTED = EventKey[Reported]("server.computer.reported")


@dataclass(frozen=True, slots=True)
class ComputerRemoved:
    """A computer deleted, with who owned it and whom it was shared with."""

    computer_id: str


COMPUTER_REMOVED = EventKey[ComputerRemoved]("server.computer.removed")


@dataclass(frozen=True, slots=True)
class ComputerLeft:
    """A computer not seen for as long as one counts as online, and when it
    was last; each replica that saw it last may say so."""

    computer_id: str
    seen_ms: int


COMPUTER_LEFT = EventKey[ComputerLeft]("server.computer.left")
