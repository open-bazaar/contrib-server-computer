"""The computers' pages: the list, connecting a new computer, one computer's
detail, removing and sharing it, and the stalls on the overview; and what
they follow through the update bus."""

from __future__ import annotations

from dataclasses import dataclass
lazy import hashlib
lazy import json
lazy from collections.abc import Awaitable, Callable, Mapping, Sequence
lazy from time import time_ns

lazy from bazaar_contrib_server import Account, Pages, Server
lazy from bazaar_contrib_server_access import Access
lazy from jinja2 import pass_context
lazy from jinja2.runtime import Context
lazy from starlette.exceptions import HTTPException
lazy from starlette.requests import Request
lazy from starlette.responses import Response
lazy from starlette.routing import Route

lazy from .calls import Calls
lazy from .contracts import Relay
lazy from .hosts import Host, Hosts
lazy from .starlink import is_online
lazy from .store import NAME_CHARS, Computer, Store

VIEW = "computers.view"
CREATE = "computers.create"
UPDATE = "computers.update"
DELETE = "computers.delete"
SHARE = "computers.share"
# the units a size is written in, each 1024 of the one before
_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


@dataclass(frozen=True, slots=True)
class Row:
    """A computer as a page shows it: whether it is here, when it last was,
    what its node last said about its machine and when, and what the
    account may do with it."""

    computer: Computer
    online: bool
    seen_ms: int | None
    host: Host | None
    known_ms: int | None
    update: bool
    delete: bool
    share: bool


def _number(value: float) -> str:
    return f"{value:.0f}" if value >= 100 else f"{value:.1f}".removesuffix(".0")


def size(used: int, total: int) -> str:
    """Used and total in the unit the total takes, as "25.1 / 32 GB"."""
    unit = 0
    while unit < len(_UNITS) - 1 and total >= 1024 ** (unit + 1):
        unit += 1
    scale = 1024**unit
    return f"{_number(used / scale)} / {_number(total / scale)} {_UNITS[unit]}"


@pass_context
def uptime(context: Context, started_ms: int) -> str:
    """How long since the node started: days and hours, hours and minutes,
    or minutes, in the words of the page's translator."""
    minutes = max(0, (time_ns() // 1_000_000 - started_ms) // 60_000)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    key = (
        "computers.uptime.days"
        if days
        else "computers.uptime.hours"
        if hours
        else "computers.uptime.minutes"
    )
    return context["t"].text(key, {"days": days, "hours": hours, "minutes": minutes})


@pass_context
def lasting(context: Context, since_ms: int) -> str:
    """How long since then in its largest unit: minutes, hours or days."""
    minutes = max(1, (time_ns() // 1_000_000 - since_ms) // 60_000)
    hours, days = minutes // 60, minutes // 1440
    key, n = (
        ("computers.lasting.days", days)
        if days
        else ("computers.lasting.hours", hours)
        if hours
        else ("computers.lasting.minutes", minutes)
    )
    return context["t"].text(key, {"n": n})


class Views:
    def __init__(
        self,
        server: Server,
        pages: Pages,
        access: Access,
        store: Store,
        relay: Relay,
        hosts: Hosts,
        calls: Calls,
        removed: Callable[[str], Awaitable[None]],
    ) -> None:
        self._server = server
        self._pages = pages
        self._access = access
        self._store = store
        self._relay = relay
        self._hosts = hosts
        self._calls = calls
        self._removed = removed
        pages.environment.filters |= {
            "size": size,
            "uptime": uptime,
            "lasting": lasting,
        }
        self.routes = [
            Route("/", self._list),
            Route("/", self._enrol, methods=["POST"]),
            Route("/table", self._table),
            Route("/more", self._more),
            Route("/enrol", self._enrolling),
            Route("/stall", self._stall),
            Route("/{ref}", self._detail),
            Route("/{ref}/name", self._rename, methods=["POST"]),
            Route("/{ref}/share", self._share),
            Route("/{ref}/remove", self._removing),
            Route("/{ref}/remove", self._remove, methods=["POST"]),
        ]

    async def name(self, computer_id: str) -> str | None:
        """What a computer is called, for its share page."""
        computer = await self._store.get(computer_id)
        return None if computer is None else computer.name

    async def _shown(
        self, account: Account, computers: Sequence[Computer]
    ) -> list[Row]:
        """The computers as the account sees them, in a fixed number of
        lookups however many there are."""
        ids = [computer.id for computer in computers]
        updatable = await self._access.targets(account, "computer", UPDATE)
        deletable = await self._access.targets(account, "computer", DELETE)
        shareable = await self._access.targets(account, "computer", SHARE)
        seen = await self._relay.last_seen(ids)
        await self._server.refs.load(ids)
        now = time_ns() // 1_000_000
        rows = []
        for computer in computers:
            online = is_online(seen.get(computer.id), now)
            known = self._hosts.of(computer.id, online)
            rows.append(
                Row(
                    computer=computer,
                    online=online,
                    seen_ms=seen.get(computer.id),
                    host=None if known is None else known[1],
                    known_ms=None if known is None else known[0],
                    update=account.allows(UPDATE)
                    and (updatable is None or computer.id in updatable),
                    delete=account.allows(DELETE)
                    and (deletable is None or computer.id in deletable),
                    share=account.allows(SHARE)
                    and (shareable is None or computer.id in shareable),
                )
            )
        return rows

    async def _visible(self, request: Request) -> frozenset[str] | None:
        account: Account = request.state.account
        if not account.allows(VIEW):
            raise HTTPException(status_code=403)
        return await self._access.targets(account, "computer", VIEW)

    async def _named(self, request: Request, key: str) -> str | None:
        """The computer a query parameter names by its ref."""
        short = request.query_params.get(key)
        found = None if short is None else await self._server.refs.values([short])
        return None if found is None else found[0]

    async def _one(self, request: Request, point: str = VIEW) -> Row:
        """The computer a page is about, for an account that may use the
        point on it; any other is not there."""
        account: Account = request.state.account
        if not account.allows(point):
            raise HTTPException(status_code=403)
        found = await self._server.refs.values([request.path_params["ref"]])
        computer = None if found is None else await self._store.get(found[0])
        if computer is None or not await self._access.can(
            account, "computer", computer.id, point
        ):
            raise HTTPException(status_code=404)
        return (await self._shown(account, [computer]))[0]

    async def _listing(self, request: Request, template: str) -> Response:
        account: Account = request.state.account
        visible = await self._visible(request)
        computers, more = await self._store.page(
            visible, until=await self._named(request, "until")
        )
        ids = list(await self._store.names(visible))
        seen = await self._relay.last_seen(ids)
        now = time_ns() // 1_000_000
        return await self._pages.render(
            request,
            template,
            rows=await self._shown(account, computers),
            more=more,
            total=len(ids),
            online=sum(1 for at in seen.values() if is_online(at, now)),
            state=await self.listed(account, {}),
            create=account.allows(CREATE),
            # a refresh brings the count in the top bar along
            refresh=template == "computers/table.html",
        )

    async def _list(self, request: Request) -> Response:
        return await self._listing(request, "computers/list.html")

    async def _table(self, request: Request) -> Response:
        """The table alone, for its refresh."""
        return await self._listing(request, "computers/table.html")

    async def _more(self, request: Request) -> Response:
        """The rows of the next page, for the scroll."""
        visible = await self._visible(request)
        computers, more = await self._store.page(
            visible, after=await self._named(request, "after")
        )
        return await self._pages.render(
            request,
            "computers/rows.html",
            rows=await self._shown(request.state.account, computers),
            more=more,
        )

    async def _enrolling(self, request: Request) -> Response:
        if not request.state.account.allows(CREATE):
            raise HTTPException(status_code=403)
        return await self._pages.render(
            request, "computers/enrol.html", enrolled=None, token=None, state=None
        )

    async def _enrol(self, request: Request) -> Response:
        account: Account = request.state.account
        if not account.allows(CREATE):
            raise HTTPException(status_code=403)
        name = str((await request.form()).get("name", "")).strip()[:NAME_CHARS]
        if not name:
            return await self._pages.render(
                request,
                "computers/enrol.html",
                status_code=422,
                enrolled=None,
                token=None,
                state=None,
            )
        computer, token = await self._store.enrol(name, account.id)
        await self._server.refs.load([computer.id])
        return await self._pages.render(
            request,
            "computers/enrol.html",
            enrolled=computer,
            token=token,
            state=await self.computer(
                account, {"computer": str(self._server.refs.ref(computer.id))}
            ),
        )

    async def _detail(self, request: Request, *, invalid: bool = False) -> Response:
        row = await self._one(request)
        return await self._pages.render(
            request,
            "computers/detail.html",
            status_code=422 if invalid else 200,
            row=row,
            # the row's menu opens it with the name being changed
            editing=invalid or (row.update and "edit" in request.query_params),
            invalid=invalid,
            state=await self.computer(
                request.state.account, {"computer": request.path_params["ref"]}
            ),
        )

    async def _rename(self, request: Request) -> Response:
        row = await self._one(request, UPDATE)
        name = str((await request.form()).get("name", "")).strip()[:NAME_CHARS]
        if not name:
            return await self._detail(request, invalid=True)
        if not await self._store.rename(row.computer.id, name):
            raise HTTPException(status_code=404)
        return await self._detail(request)

    async def _share(self, request: Request) -> Response:
        return await self._pages.render(
            request, "computers/share.html", row=await self._one(request, SHARE)
        )

    async def _removing(self, request: Request) -> Response:
        return await self._pages.render(
            request, "computers/remove.html", row=await self._one(request, DELETE)
        )

    async def _remove(self, request: Request) -> Response:
        row = await self._one(request, DELETE)
        if not await self._store.remove(row.computer.id):
            raise HTTPException(status_code=404)
        self._hosts.forget(row.computer.id)
        await self._removed(row.computer.id)
        # the page showing it lists again
        return Response(status_code=204, headers={"HX-Trigger": "computers:removed"})

    async def _stall(self, request: Request) -> Response:
        """The overview's stalls: one a computer, with what another module
        shows in it, and how many are away."""
        visible = await self._visible(request)
        computers, more = await self._store.page(visible)
        ids = list(await self._store.names(visible))
        seen = await self._relay.last_seen(ids)
        now = time_ns() // 1_000_000
        return await self._pages.render(
            request,
            "computers/stall.html",
            rows=await self._shown(request.state.account, computers),
            more=more,
            total=len(ids),
            away=sum(
                1 for computer_id in ids if not is_online(seen.get(computer_id), now)
            ),
            state=await self.listed(request.state.account, {}),
            goods=self._calls.goods,
            create=request.state.account.allows(CREATE),
        )

    async def counted(
        self, account: Account, scope: Mapping[str, str]
    ) -> dict[str, str]:
        """The sidebar's badge: how many computers the account sees."""
        del scope
        names = await self._store.names(
            await self._access.targets(account, "computer", VIEW)
        )
        return {"text": str(len(names)) if names else "", "kind": "count"}

    async def listed(
        self, account: Account, scope: Mapping[str, str]
    ) -> dict[str, str]:
        """What the list shows, as a digest: which computers by what name,
        whether each is here, and when its node last said what about its
        machine."""
        del scope
        names = await self._store.names(
            await self._access.targets(account, "computer", VIEW)
        )
        ids = sorted(names)
        seen = await self._relay.last_seen(ids)
        now = time_ns() // 1_000_000
        shown = []
        for computer_id in ids:
            online = is_online(seen.get(computer_id), now)
            known = self._hosts.of(computer_id, online)
            shown.append(
                [
                    computer_id,
                    names[computer_id],
                    online,
                    None if known is None else known[0],
                ]
            )
        return {
            "digest": hashlib.blake2b(
                json.dumps(shown).encode(), digest_size=12
            ).hexdigest()
        }

    async def computer(
        self, account: Account, scope: Mapping[str, str]
    ) -> dict[str, object] | None:
        """One computer as its detail shows it: whether it is here, and when
        its node last said what about its machine; nothing for one the
        account may not see."""
        found = await self._server.refs.values([scope["computer"]])
        if found is None or not await self._access.can(
            account, "computer", found[0], VIEW
        ):
            return None
        if await self._store.get(found[0]) is None:
            return None
        seen = (await self._relay.last_seen(found)).get(found[0])
        online = is_online(seen, time_ns() // 1_000_000)
        known = self._hosts.of(found[0], online)
        return {"online": online, "known": None if known is None else known[0]}
