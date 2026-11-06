"""A computer's side of the protocol over real HTTP: its token, listening for
calls, announcing their results and reporting its audit events."""

from __future__ import annotations

from contextlib import AsyncExitStack
lazy import asyncio
lazy from time import monotonic, time_ns
lazy from typing import Any
lazy from uuid import uuid7

from served import admin, client, enrol, node, serving
lazy import httpx

GIB = 1024**3
INFO = {
    "version": "0.3.0",
    "system": "Linux",
    "name": "Fedora Linux 44",
    "release": "6.19.10-300.fc44.x86_64",
    "machine": "x86_64",
    "started_ms": time_ns() // 1_000_000 - 3_600_000,
    "cpu": {"cores": 4, "percent": 12.5},
    "memory": {"total": 8 * GIB, "used": 2 * GIB},
    "disks": [{"mount": "/", "total": 40 * GIB, "used": 14 * GIB}],
}


async def connected(computer: httpx.AsyncClient) -> None:
    """Listen once, answering what the server asks a computer that comes
    online: about its machine."""
    for call in (await computer.get("/listen")).json()["calls"]:
        assert call["request"]["service"] == "node"
        assert call["request"]["method"] == "info"
        await computer.put(
            f"/announce/{call['number']}", json={"kind": "result", "value": INFO}
        )


def event(run: str, sequence: int) -> dict[str, object]:
    now = time_ns() // 1_000_000
    return {
        "level": "info",
        "id": str(uuid7()),
        "run_id": run,
        "sequence": sequence,
        "event_name": "agent.turn.started",
        "occurred_at_ms": now,
        "observed_at_ms": now,
        "monotonic_ns": 1,
        "context": {"agent_id": "kana"},
        "payload": {"n": sequence},
    }


async def test_a_computer_connects_with_its_token_alone_and_shows_its_machine():
    async with serving() as served, client() as boss:
        await admin(served, boss)
        _, token = await enrol(served, boss, "kana-vm")
        _, other = await enrol(served, boss, "aqua-mbp")
        computer_id, _, secret = token.partition(":")
        for wrong in (
            f"{computer_id}:not-it",
            secret,
            f"{other.partition(':')[0]}:{secret}",
        ):
            async with node(served, wrong) as stranger:
                assert (await stranger.get("/listen")).status_code == 401
        async with node(served, token) as computer:
            listed = await boss.get(f"{served.url}/ui/computers/")
            assert "kana-vm" in listed.text
            assert "Fedora Linux 44" not in listed.text
            await connected(computer)
            listed = await boss.get(f"{served.url}/ui/computers/")
            assert "Fedora Linux 44" in listed.text
            assert "25%" in listed.text


async def test_calls_reach_the_computer_once_however_many_wait():
    async with serving() as served, client() as boss:
        await admin(served, boss)
        ref, token = await enrol(served, boss, "kana-vm")
        async with node(served, token) as computer:
            await connected(computer)
            asked = [
                asyncio.create_task(
                    boss.post(f"{served.url}/ui/probe/{ref}/echo", json=[1])
                )
                for _ in range(2)
            ]
            different = asyncio.create_task(
                boss.post(f"{served.url}/ui/probe/{ref}/echo", json=[2])
            )
            calls: list[dict[str, Any]] = []
            while len(calls) < 2:
                calls += (await computer.get("/listen")).json()["calls"]
            # the same call while one is out is handed over once
            assert sorted(call["request"]["args"] for call in calls) == [[1], [2]]
            for call in calls:
                await computer.put(
                    f"/announce/{call['number']}",
                    json={"kind": "result", "value": call["request"]["args"]},
                )
                # an answer given again counts once
                await computer.put(
                    f"/announce/{call['number']}",
                    json={"kind": "result", "value": "again"},
                )
            assert [(await task).json() for task in asked] == [{"value": [1]}] * 2
            assert (await different).json() == {"value": [2]}
            # what failed on the computer fails for the caller
            failing = asyncio.create_task(
                boss.post(f"{served.url}/ui/probe/{ref}/fail", json=[])
            )
            (call,) = (await computer.get("/listen")).json()["calls"]
            await computer.put(
                f"/announce/{call['number']}",
                json={"kind": "error", "type": "LookupError", "message": "no"},
            )
            assert (await failing).status_code == 502
            assert (
                await computer.put(f"/announce/{call['number']}", content=b"{")
            ).status_code == 422


async def test_a_listen_is_held_a_call_waits_a_minute_and_both_end_with_the_server():
    async with AsyncExitStack() as stack:
        async with serving() as served, client() as boss:
            await admin(served, boss)
            quiet_ref, quiet_token = await enrol(served, boss, "quiet")
            _, token = await enrol(served, boss, "kana-vm")
            computer = await stack.enter_async_context(node(served, token))
            await connected(computer)

            async def held() -> float:
                started = monotonic()
                assert (await computer.get("/listen")).json() == {"calls": []}
                return monotonic() - started

            async def unanswered() -> httpx.Response:
                # a computer that never listens leaves its call unanswered
                return await boss.post(
                    f"{served.url}/ui/probe/{quiet_ref}/echo", json=[1]
                )

            seconds, silent = await asyncio.gather(held(), unanswered())
            assert seconds >= 20
            assert silent.status_code == 504
            quiet = await stack.enter_async_context(node(served, quiet_token))
            await connected(quiet)
            waiting = asyncio.create_task(quiet.get("/listen"))
            await asyncio.sleep(1)
            stopping = monotonic()
        # the server went down with the listen held: it was let go, not cut off
        assert (await waiting).json() == {"calls": []}
        assert monotonic() - stopping < 10


async def test_reported_events_are_announced_once_and_confirmed():
    async with serving() as served, client() as boss:
        await admin(served, boss)
        _, token = await enrol(served, boss, "kana-vm")
        async with node(served, token) as computer:
            run = str(uuid7())
            sent = await computer.post(
                "/report",
                json={"run_id": run, "events": [event(run, 1), event(run, 2)]},
            )
            assert sent.json() == {"run_id": run, "sequence": 2}
            # a batch sent again after its answer was lost adds what is new
            again = await computer.post(
                "/report",
                json={"run_id": run, "events": [event(run, 2), event(run, 3)]},
            )
            assert again.json() == {"run_id": run, "sequence": 3}
            assert [heard.event.sequence for heard in served.reported] == [1, 2, 3]
            assert {heard.computer_id for heard in served.reported} == {
                token.partition(":")[0]
            }
            # a computer started again reports from its new run's beginning
            later = str(uuid7())
            fresh = await computer.post(
                "/report", json={"run_id": later, "events": [event(later, 1)]}
            )
            assert fresh.json() == {"run_id": later, "sequence": 1}
            assert len(served.reported) == 4
            mixed = await computer.post(
                "/report", json={"run_id": run, "events": [event(later, 2)]}
            )
            assert mixed.status_code == 422
            too_large = await computer.post("/report", content=b"x" * (1024 * 1024 + 1))
            assert too_large.status_code == 413
            assert len(served.reported) == 4
