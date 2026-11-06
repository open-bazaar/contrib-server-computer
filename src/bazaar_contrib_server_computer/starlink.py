"""Where a computer's starlink connects: it listens for the calls waiting for
it, announces their results and reports its audit events, each request on
its own and with the computer's token."""

from __future__ import annotations

lazy import asyncio
lazy from collections.abc import Awaitable, Callable, Sequence
lazy from time import time_ns
lazy from typing import Annotated, Self

lazy from bazaar_contrib_audit import AuditEvent
lazy from bazaar_contrib_sidecar.wire import Failure, Result
lazy from pydantic import (
    BaseModel,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)
lazy from starlette.requests import Request
lazy from starlette.responses import JSONResponse, Response

from .contracts import Call, Relay, Reported
lazy from .hosts import Hosts
lazy from .store import Computer, Store

# a computer is online while it was here within this long: a listen is held
# 25 seconds, and one that failed is tried again within the minute
ONLINE_MS = 60_000
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


async def _body(request: Request) -> bytes | None:
    """What the request carries, nothing when it is more than one may."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return body


class Starlink:
    def __init__(
        self,
        store: Store,
        relay: Relay,
        hosts: Hosts,
        announce: Callable[[Reported], Awaitable[None]],
    ) -> None:
        self._store = store
        self._relay = relay
        self._hosts = hosts
        self._announce = announce
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
        before = await self._relay.seen(computer.id)
        if before is None or time_ns() // 1_000_000 - before > ONLINE_MS:
            self._hosts.ask(computer.id)
        return computer

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
        """Let the held listens go with nothing: the server is going down."""
        self._stopping = True
        loop = asyncio.get_running_loop()
        for scope in self._held:
            scope.reschedule(loop.time())
