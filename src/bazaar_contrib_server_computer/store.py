"""The computers: enrolling one with the token it connects with, finding,
listing and removing them."""

from __future__ import annotations

from dataclasses import dataclass
lazy from time import time_ns
lazy from typing import cast
lazy from uuid import uuid7

lazy from bazaar_contrib_server_access import (
    Access,
    derive,
    hash_secret,
    new_secret,
    verify_secret,
)
lazy from bazaar_contrib_storage import (
    All,
    Column,
    Compare,
    Database,
    Delete,
    In,
    Insert,
    Predicate,
    Record,
    Select,
    Session,
    Sort,
    Update,
    Value,
)

# what one page of the list holds
PAGE = 50
# a name is cut to what a row shows, as 0.2 did
NAME_CHARS = 100


@dataclass(frozen=True, slots=True)
class Computer:
    id: str
    name: str
    created_at_ms: int


def _computer(row: Record) -> Computer:
    return Computer(
        id=cast(str, row["id"]),
        name=cast(str, row["name"]),
        created_at_ms=cast(int, row["created_at_ms"]),
    )


def _equals(column: str, value: str) -> Compare:
    return Compare(left=Column(name=column), operator="=", right=Value(value=value))


def _among(ids: frozenset[str] | None) -> list[Predicate]:
    """The condition keeping to the computers given; none for every one."""
    return [] if ids is None else [In(expression=Column(name="id"), values=tuple(ids))]


class Store:
    def __init__(self, database: Database, access: Access) -> None:
        self._database = database
        self._access = access

    async def enrol(self, name: str, owner_id: str) -> tuple[Computer, str]:
        """A new computer the account owns, and the token it connects with,
        given this once: the computer's id and a secret only its digest of
        which is kept."""
        computer = Computer(
            id=str(uuid7()), name=name, created_at_ms=time_ns() // 1_000_000
        )
        secret = new_secret()
        digest = await derive(hash_secret, secret)

        async def add(session: Session) -> None:
            await session.insert(
                Insert(
                    table="computers",
                    rows=(
                        {
                            "id": computer.id,
                            "name": computer.name,
                            "secret_hash": digest,
                            "created_at_ms": computer.created_at_ms,
                        },
                    ),
                )
            )
            await self._access.own(session, owner_id, "computer", computer.id)

        await self._database.transaction(add)
        return computer, f"{computer.id}:{secret}"

    async def authenticate(self, token: str) -> Computer | None:
        """The computer a token speaks for, nothing for any other token."""
        computer_id, separator, secret = token.partition(":")
        if not separator or not computer_id or not secret:
            return None
        rows = await self._database.read(
            lambda session: session.select(
                Select(table="computers", where=_equals("id", computer_id))
            )
        )
        if not rows or not await derive(
            verify_secret, secret, cast(str, rows[0]["secret_hash"])
        ):
            return None
        return _computer(rows[0])

    async def get(self, computer_id: str) -> Computer | None:
        rows = await self._database.read(
            lambda session: session.select(
                Select(table="computers", where=_equals("id", computer_id))
            )
        )
        return _computer(rows[0]) if rows else None

    async def page(
        self,
        ids: frozenset[str] | None,
        *,
        after: str | None = None,
        until: str | None = None,
    ) -> tuple[list[Computer], bool]:
        """The computers among those given, in the order they came: a page
        past the one named, or all as far as the one named; and whether more
        follow."""
        if ids is not None and not ids:
            return [], False
        conditions = _among(ids)
        if after is not None:
            conditions.append(
                Compare(left=Column(name="id"), operator=">", right=Value(value=after))
            )
        if until is not None:
            conditions.append(
                Compare(left=Column(name="id"), operator="<=", right=Value(value=until))
            )
        rows = await self._database.read(
            lambda session: session.select(
                Select(
                    table="computers",
                    columns={
                        name: Column(name=name)
                        for name in ("id", "name", "created_at_ms")
                    },
                    where=All(conditions=tuple(conditions)) if conditions else None,
                    order_by=(Sort(expression=Column(name="id")),),
                    limit=None if until is not None else PAGE + 1,
                )
            )
        )
        if until is None:
            return [_computer(row) for row in rows[:PAGE]], len(rows) > PAGE
        later, _ = await self.page(ids, after=until)
        return [_computer(row) for row in rows], bool(later)

    async def names(self, ids: frozenset[str] | None) -> dict[str, str]:
        """What each of the computers given is called, of those that are
        there; every one for nothing."""
        if ids is not None and not ids:
            return {}
        conditions = _among(ids)
        rows = await self._database.read(
            lambda session: session.select(
                Select(
                    table="computers",
                    columns={"id": Column(name="id"), "name": Column(name="name")},
                    where=All(conditions=tuple(conditions)) if conditions else None,
                )
            )
        )
        return {cast(str, row["id"]): cast(str, row["name"]) for row in rows}

    async def rename(self, computer_id: str, name: str) -> bool:
        renamed = await self._database.write(
            lambda session: session.update(
                Update(
                    table="computers",
                    values={"name": Value(value=name)},
                    where=_equals("id", computer_id),
                )
            )
        )
        return renamed.count > 0

    async def remove(self, computer_id: str) -> bool:
        """The computer gone, with who owned it and whom it was shared with;
        nothing to remove says so."""

        async def remove(session: Session) -> bool:
            await self._access.forget(session, "computer", computer_id)
            removed = await session.delete(
                Delete(table="computers", where=_equals("id", computer_id))
            )
            return removed.count > 0

        return await self._database.transaction(remove)
