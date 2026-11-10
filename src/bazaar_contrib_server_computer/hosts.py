"""What each computer's node says about the machine it runs on: asked when
the computer comes online, and again once a minute old while a page shows
it."""

from __future__ import annotations

lazy import asyncio
lazy import logging
lazy from time import time_ns

from pydantic import BaseModel, ValidationError
lazy from bazaar_contrib_sidecar import RemoteError

from .contracts import Computers

# what was said is shown for a minute before the node is asked again
FRESH_MS = 60_000


class Usage(BaseModel):
    total: int
    used: int

    @property
    def percent(self) -> int:
        return round(self.used * 100 / self.total) if self.total else 0


class Disk(Usage):
    mount: str


class Processor(BaseModel):
    cores: int | None
    percent: float


class Host(BaseModel):
    """node.info as a node answers it."""

    version: str
    system: str
    name: str
    release: str
    machine: str
    started_ms: int
    cpu: Processor
    memory: Usage
    disks: list[Disk]

    @property
    def fullest(self) -> Disk | None:
        return max(self.disks, key=lambda disk: disk.percent, default=None)


class Hosts:
    def __init__(self, computers: Computers, tasks: asyncio.TaskGroup) -> None:
        self._computers = computers
        self._tasks = tasks
        self._known: dict[str, tuple[int, Host]] = {}
        self._asking: dict[str, asyncio.Task[None]] = {}

    def ask(self, computer_id: str) -> None:
        """Ask the computer about its machine again, unless that is under way."""
        if computer_id not in self._asking:
            self._asking[computer_id] = self._tasks.create_task(self._ask(computer_id))

    async def _ask(self, computer_id: str) -> None:
        try:
            value = await self._computers.call(computer_id, "node", "info")
            self._known[computer_id] = (
                time_ns() // 1_000_000,
                Host.model_validate(value),
            )
        except (RemoteError, TimeoutError, ValidationError) as error:
            # a node that does not answer, or answers otherwise, keeps what
            # was said before
            logging.getLogger(__name__).info("computer.info.unanswered: %r", error)
        finally:
            del self._asking[computer_id]

    def of(self, computer_id: str, online: bool) -> tuple[int, Host] | None:
        """When the computer last said what about its machine; one online
        is asked again once that is a minute old."""
        known = self._known.get(computer_id)
        if online and (known is None or time_ns() // 1_000_000 - known[0] >= FRESH_MS):
            self.ask(computer_id)
        return known

    def forget(self, computer_id: str) -> None:
        self._known.pop(computer_id, None)

    def close(self) -> None:
        """Stop asking: the server is going down."""
        for task in self._asking.values():
            task.cancel()
