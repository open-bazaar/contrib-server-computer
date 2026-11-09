"""server-relay-memory: the relay of a server with one replica, kept in its
own memory, as 0.2's controls were."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
lazy import asyncio
lazy import json
lazy from collections.abc import Sequence
lazy from time import time_ns

lazy from bazaar_contrib_sidecar.wire import Failure, Request, Result
lazy from bazaar_contrib_timer import Timer, timeout

from .contracts import ANSWER_MS, CALLS_AT_MOST, HOLD_MS, Call


@dataclass(slots=True)
class _Asked:
    computer_id: str
    call: Call
    answer: asyncio.Future[Result | Failure]
    # how many askers wait on the answer; none, and the call is forgotten
    waiters: int = 0


class MemoryRelay:
    def __init__(self, timer: Timer) -> None:
        self._timer = timer
        # what each computer has been asked and not yet answered, by number
        self._asked: defaultdict[str, dict[int, _Asked]] = defaultdict(dict)
        # the same call to the same computer, while one is out, is asked once
        self._out: dict[tuple[str, str], _Asked] = {}
        # a listen waits here until its computer has something
        self._arrivals: defaultdict[str, asyncio.Condition] = defaultdict(
            asyncio.Condition
        )
        self._seen: dict[str, int] = {}
        # numbers climb with the clock, so where a computer got to before the
        # server restarted is behind what comes after
        self._last = 0
        self._closing = False

    async def ask(self, computer_id: str, request: Request) -> Result | Failure:
        key = (
            computer_id,
            json.dumps(request.model_dump(mode="json"), sort_keys=True),
        )
        asked = self._out.get(key)
        fresh = asked is None
        if asked is None:
            number = self._last = max(self._last + 1, time_ns() // 1_000_000)
            asked = _Asked(
                computer_id,
                Call(number, request),
                asyncio.get_running_loop().create_future(),
            )
            self._out[key] = asked
            self._asked[computer_id][number] = asked
            asked.answer.add_done_callback(lambda _: self._forget(key, asked))
        asked.waiters += 1
        try:
            if fresh:
                arrivals = self._arrivals[computer_id]
                async with arrivals:
                    arrivals.notify_all()
            async with timeout(self._timer, ANSWER_MS):
                # one asker giving up does not take the answer from the others
                return await asyncio.shield(asked.answer)
        finally:
            asked.waiters -= 1
            if asked.waiters == 0 and not asked.answer.done():
                self._forget(key, asked)

    def _forget(self, key: tuple[str, str], asked: _Asked) -> None:
        if self._out.get(key) is asked:
            del self._out[key]
        self._asked[asked.computer_id].pop(asked.call.number, None)

    async def calls(self, computer_id: str, after: int) -> Sequence[Call]:
        arrivals = self._arrivals[computer_id]
        async with arrivals:
            if not self._closing and not self._pending(computer_id, after):
                try:
                    async with timeout(self._timer, HOLD_MS):
                        await arrivals.wait()
                except TimeoutError:
                    return []
            return [] if self._closing else self._pending(computer_id, after)

    def _pending(self, computer_id: str, after: int) -> list[Call]:
        asked = self._asked[computer_id]
        return [asked[number].call for number in sorted(asked) if number > after][
            :CALLS_AT_MOST
        ]

    async def answer(
        self, computer_id: str, number: int, response: Result | Failure
    ) -> None:
        asked = self._asked[computer_id].get(number)
        if asked is not None and not asked.answer.done():
            asked.answer.set_result(response)

    async def seen(self, computer_id: str) -> tuple[int | None, int]:
        before = self._seen.get(computer_id)
        now = self._seen[computer_id] = time_ns() // 1_000_000
        return before, now

    async def last_seen(self, computer_ids: Sequence[str]) -> dict[str, int]:
        return {
            computer_id: self._seen[computer_id]
            for computer_id in computer_ids
            if computer_id in self._seen
        }

    async def close(self) -> None:
        """Let every held listen go: the relay is going away."""
        self._closing = True
        for arrivals in list(self._arrivals.values()):
            async with arrivals:
                arrivals.notify_all()
