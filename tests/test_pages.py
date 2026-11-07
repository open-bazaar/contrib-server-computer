"""The computers' pages over real HTTP: who sees which computer, and what
they may do with it, by permission point and by share."""

from __future__ import annotations

from served import (
    Served,
    admin,
    client,
    enrol,
    node,
    role,
    serving,
    through,
)
lazy import httpx


async def follow(
    served: Served, session: httpx.AsyncClient, topic: str, **scope: str
) -> object:
    """What the update bus says about the topic, as a page would ask."""
    answer = await session.post(
        f"{served.url}/api/server/poll",
        json={
            "subscriptions": [{"id": "0", "topic": topic, "scope": scope, "seen": None}]
        },
    )
    (change,) = answer.json()["changes"]
    return change.get("state")


async def test_a_computer_shows_to_its_owner_and_to_the_roles_it_is_shared_with():
    async with (
        serving() as served,
        client() as boss,
        client() as alice,
        client() as bob,
        client() as carol,
    ):
        await admin(served, boss)
        makers = await role(
            served,
            boss,
            "computers.view",
            "computers.create",
            "computers.delete",
            "computers.share",
        )
        viewers = await role(served, boss, "computers.view")
        nobody = await role(served, boss)
        for person, session, held in (
            ("alice", alice, makers),
            ("bob", bob, viewers),
            ("carol", carol, nobody),
        ):
            await boss.post(f"{served.url}{held}/default")
            await through(served, session, person)
        north, token = await enrol(served, alice, "north")
        assert "north" in (await alice.get(f"{served.url}/ui/computers/")).text
        assert await follow(served, alice, "computers.count") == {
            "text": "1",
            "kind": "count",
        }
        # another's computer is not there until it is shared
        assert "north" not in (await bob.get(f"{served.url}/ui/computers/")).text
        assert (await bob.get(f"{served.url}/ui/computers/{north}")).status_code == 404
        assert await follow(served, bob, "computers.computer", computer=north) is None
        # without the point for viewing, there are no computers at all
        assert (await carol.get(f"{served.url}/ui/computers/")).status_code == 403
        given = viewers.rsplit("/", 1)[1]
        shared = await alice.post(
            f"{served.url}/ui/access/shares/computer/{north}",
            data={"roles": [given], "grant": [f"{given} computers.view"]},
        )
        assert shared.status_code == 200
        assert "north" in (await bob.get(f"{served.url}/ui/computers/")).text
        assert (await bob.get(f"{served.url}/ui/computers/{north}")).status_code == 200
        assert "north" in (await bob.get(f"{served.url}/ui/computers/stall")).text
        assert await follow(served, bob, "computers.computer", computer=north) == {
            "online": False,
            "known": None,
        }
        # shared for viewing: not removed, nor shared on, by those it is shared with
        assert (
            await bob.post(f"{served.url}/ui/computers/{north}/remove")
        ).status_code == 403
        assert (
            await bob.get(f"{served.url}/ui/computers/{north}/share")
        ).status_code == 403
        detail = await alice.get(f"{served.url}/ui/computers/{north}")
        assert f"/ui/computers/{north}/share" in detail.text
        assert f"/ui/computers/{north}/remove" in detail.text
        detail = await bob.get(f"{served.url}/ui/computers/{north}")
        assert f"/ui/computers/{north}/share" not in detail.text
        assert f"/ui/computers/{north}/remove" not in detail.text
        # other modules list it by id, with its name and whether it is here
        north_id = token.partition(":")[0]
        listed = (
            await alice.get(f"{served.url}/ui/probe/computers?id={north_id}")
        ).json()
        assert listed == [{"id": north_id, "name": "north", "online": False}]
        # removed, it is gone for everyone and its token speaks for nothing
        removed = await alice.post(f"{served.url}/ui/computers/{north}/remove")
        assert removed.status_code == 204
        assert "north" not in (await bob.get(f"{served.url}/ui/computers/")).text
        assert any(event.computer_id == north_id for event in served.removed)
        everyone = (await alice.get(f"{served.url}/ui/probe/computers")).json()
        assert all(item["id"] != north_id for item in everyone)
        async with node(served, token) as computer:
            assert (await computer.get("/listen")).status_code == 401


async def test_connecting_takes_a_name_and_the_point_for_it():
    async with serving() as served, client() as boss, client() as bob:
        await admin(served, boss)
        viewers = await role(served, boss, "computers.view")
        await boss.post(f"{served.url}{viewers}/default")
        await through(served, bob, "bob")
        assert (
            await bob.post(f"{served.url}/ui/computers/", data={"name": "mine"})
        ).status_code == 403
        assert (
            await boss.post(f"{served.url}/ui/computers/", data={"name": "  "})
        ).status_code == 422
        # a long name is cut to what a row shows
        _, token = await enrol(served, boss, "x" * 300)
        listed = await boss.get(f"{served.url}/ui/computers/")
        assert "x" * 100 in listed.text
        assert "x" * 101 not in listed.text
        assert token


async def test_a_computer_is_renamed_by_those_who_may_change_it():
    async with serving() as served, client() as boss, client() as bob:
        await admin(served, boss)
        changers = await role(served, boss, "computers.view", "computers.update")
        await boss.post(f"{served.url}{changers}/default")
        await through(served, bob, "bob")
        north, _ = await enrol(served, boss, "north")
        given = changers.rsplit("/", 1)[1]
        sharing = f"{served.url}/ui/access/shares/computer/{north}"
        renaming = f"{served.url}/ui/computers/{north}/name"
        # shared for viewing alone, it is not bob's to change
        await boss.post(
            sharing, data={"roles": [given], "grant": [f"{given} computers.view"]}
        )
        assert (await bob.post(renaming, data={"name": "south"})).status_code == 404
        await boss.post(
            sharing,
            data={
                "roles": [given],
                "grant": [f"{given} computers.view", f"{given} computers.update"],
            },
        )
        assert (await bob.post(renaming, data={"name": "  south  "})).status_code == 200
        listed = (await boss.get(f"{served.url}/ui/computers/")).text
        assert "south" in listed
        assert "north" not in listed
        assert (await bob.post(renaming, data={"name": " "})).status_code == 422
        assert "south" in (await boss.get(f"{served.url}/ui/computers/")).text
