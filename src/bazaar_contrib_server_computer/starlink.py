"""Where a computer's starlink connects: it listens for the calls waiting for
it, announces their results and reports its audit events, each request on
its own and with the computer's token; one that stops coming is said to
have left once it has been away as long as one counts as online."""

from __future__ import annotations

from dataclasses import dataclass
lazy import asyncio
lazy from collections.abc import Awaitable, Callable, Sequence
lazy from typing import Annotated, Self

lazy from bazaar_contrib_audit import AuditEvent
lazy from bazaar_contrib_sidecar.wire import Failure, Result
lazy from bazaar_contrib_timer import (
    Timer,
    TimerCancelledError,
    TimerClosedError,
    TimerHandle,
)
lazy from pydantic import (
    BaseModel,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)
lazy from starlette.requests import ClientDisconnect, Request
lazy from starlette.responses import JSONResponse, Response

from .contracts import Call, ComputerLeft, Relay, Reported
lazy from .hosts import Hosts
lazy from .store import Computer, Store

# a computer is online while it was here within this long: a listen is held
# 25 seconds, and one that failed is tried again within the minute
ONLINE_MS = 60_000


def is_online(seen_ms: int | None, now_ms: int) -> bool:
    return seen_ms is not None and now_ms - seen_ms <= ONLINE_MS


# what one request may carry, as the server's own addresses take
MAX_BODY_BYTES = 1024 * 1024

ANSWER = TypeAdapter[Result | Failure](
    Annotated[Result | Failure, Field(discriminator="kind")]
)
AFTER = TypeAdapter[int](Annotated[int, Field(ge=0)])


class _Report(BaseModel):
    run_id: str
    events: list[AuditEvent]

    @model_validator(mode="after")
    def one_run(self) -> Self:
        if any(event.run_id != self.run_id for event in self.events):
            raise ValueError("a report carries the events of its own run")
        return self


@dataclass(slots=True)
class _Watched:
    """The check on whether a computer left, and when the relay noted it as
    it last showed up here."""

    handle: TimerHandle
    seen_ms: int


async def _body(request: Request) -> bytes | None:
    """What the request carries; nothing when it is more than one may, or
    the computer hung up before sending all of it."""
    body = b""
    try:
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY_BYTES:
                return None
    except ClientDisconnect:
        return None
    return body


class Starlink:
    def __init__(
        self,
        store: Store,
        relay: Relay,
        hosts: Hosts,
        timer: Timer,
        tasks: asyncio.TaskGroup,
        announce: Callable[[Reported], Awaitable[None]],
        left: Callable[[ComputerLeft], Awaitable[None]],
    ) -> None:
        self._store = store
        self._relay = relay
        self._hosts = hosts
        self._timer = timer
        self._tasks = tasks
        self._announce = announce
        self._left = left
        # the check on each computer that showed up here, put off as it does again
        self._watched: dict[str, _Watched] = {}
        # the run each computer last reported from, and how far it is confirmed
        self._runs: dict[str, tuple[str, int]] = {}
        # the listens held now, let go when the server stops
        self._held: set[asyncio.Timeout] = set()
        self._stopping = False

    async def _computer(self, request: Request) -> Computer | None:
        """The computer the request is from, nothing for a token that speaks
        for none; one that was away is asked about its machine."""
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        computer = (
            await self._store.authenticate(token.strip())
            if scheme.lower() == "bearer"
            else None
        )
        if computer is None:
            return None
        before, now = await self._relay.seen(computer.id)
        if before is None or now - before > ONLINE_MS:
            self._hosts.ask(computer.id)
        self._watch(computer.id, now)
        return computer

    def _watch(self, computer_id: str, seen_ms: int) -> None:
        """Put the check on whether the computer left off until it has been
        away ONLINE_MS from now."""
        if self._stopping:
            return
        watched = self._watched.get(computer_id)
        if watched is not None and watched.handle.active:
            # the relay's times only move on, whichever answer comes back first
            watched.seen_ms = max(watched.seen_ms, seen_ms)
            watched.handle.reset(ONLINE_MS)
            return
        watched = self._watched[computer_id] = _Watched(
            self._timer.create(ONLINE_MS), seen_ms
        )
        self._tasks.create_task(self._check(computer_id, watched))

    async def _check(self, computer_id: str, watched: _Watched) -> None:
        """The computer left when the relay has not seen it since it was last
        here: on any replica it showed up at later, the relay would have
        noted another time."""
        try:
            await watched.handle.wait()
        except TimerCancelledError, TimerClosedError:
            return
        finally:
            if self._watched.get(computer_id) is watched:
                del self._watched[computer_id]
        seen = (await self._relay.last_seen([computer_id])).get(computer_id)
        if seen == watched.seen_ms:
            await self._left(ComputerLeft(computer_id, watched.seen_ms))

    async def listen(self, request: Request) -> Response:
        computer = await self._computer(request)
        if computer is None:
            return Response(status_code=401)
        try:
            after = AFTER.validate_python(request.query_params.get("after", "0"))
        except ValidationError:
            return Response(status_code=422)
        calls: Sequence[Call] = ()
        if not self._stopping:
            scope = asyncio.timeout(None)
            self._held.add(scope)
            try:
                async with scope:
                    calls = await self._relay.calls(computer.id, after)
            except TimeoutError:
                if not scope.expired():
                    raise
            finally:
                self._held.discard(scope)
        return JSONResponse(
            {
                "calls": [
                    {
                        "number": call.number,
                        "request": call.request.model_dump(mode="json"),
                    }
                    for call in calls
                ]
            }
        )

    async def announce(self, request: Request) -> Response:
        computer = await self._computer(request)
        if computer is None:
            return Response(status_code=401)
        body = await _body(request)
        if body is None:
            return Response(status_code=413)
        try:
            response = ANSWER.validate_json(body)
        except ValidationError:
            return Response(status_code=422)
        await self._relay.answer(computer.id, request.path_params["number"], response)
        return Response(status_code=204)

    async def report(self, request: Request) -> Response:
        computer = await self._computer(request)
        if computer is None:
            return Response(status_code=401)
        body = await _body(request)
        if body is None:
            return Response(status_code=413)
        try:
            report = _Report.model_validate_json(body)
        except ValidationError:
            return Response(status_code=422)
        run, confirmed = self._runs.get(computer.id, (report.run_id, 0))
        if run != report.run_id:
            # the computer started again: a run's events go no further than it
            confirmed = 0
        for event in sorted(report.events, key=lambda event: event.sequence):
            # what was confirmed already is a replay, announced once before
            if event.sequence > confirmed:
                await self._announce(Reported(computer_id=computer.id, event=event))
                confirmed = event.sequence
        self._runs[computer.id] = (report.run_id, confirmed)
        return JSONResponse({"run_id": report.run_id, "sequence": confirmed})

    def stop(self) -> None:
        """Let the held listens go with nothing, and drop the checks not yet
        due: the server is going down."""
        self._stopping = True
        loop = asyncio.get_running_loop()
        for scope in self._held:
            scope.reschedule(loop.time())
        for watched in self._watched.values():
            watched.handle.cancel()
