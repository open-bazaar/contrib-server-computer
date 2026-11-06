"""The computers of the Bazaar Compute server: enrolling them, where their
starlink connects, calling what they expose, and their pages; and the relay
between the server's replicas, kept in memory for a server with one."""

from __future__ import annotations

from contextlib import asynccontextmanager
lazy import asyncio
lazy from collections.abc import AsyncIterator
lazy from importlib.resources import files
lazy from pathlib import Path

from bazaar_contrib_http import HTTP
from bazaar_contrib_server import SERVER, PermissionItem, SidebarItem, Topic
from bazaar_contrib_server_access import ACCESS, Shared
from bazaar_contrib_server_database import SERVER_DB, SERVER_SCHEMA
from bazaar_contrib_server_migrations import SERVER_MIGRATIONS
from bazaar_contrib_server_overview import OVERVIEW, OverviewItem
from bazaar_contrib_timer import TIMER
from bazaar_core import Context, Contrib

from .contracts import (
    ANSWER_MS,
    CALLS_AT_MOST,
    COMPUTERS,
    HOLD_MS,
    RELAY,
    REPORTED,
    Call,
    Computers,
    Relay,
    Reported,
)
lazy from .calls import Calls
lazy from .hosts import Hosts
lazy from .memory import MemoryRelay
lazy from .pages import CREATE, DELETE, SHARE, UPDATE, VIEW, Views
lazy from .schema import MODULE
lazy from .starlink import Starlink
lazy from .store import Store

__all__ = [
    "ANSWER_MS",
    "CALLS_AT_MOST",
    "COMPUTERS",
    "HOLD_MS",
    "RELAY",
    "REPORTED",
    "Call",
    "Computers",
    "Relay",
    "Reported",
    "computer",
    "relay",
]


@asynccontextmanager
async def _computer(ctx: Context) -> AsyncIterator[None]:
    server = ctx.accept(SERVER)
    access = ctx.accept(ACCESS)
    http = ctx.accept(HTTP)
    relay = ctx.accept(RELAY)
    resources = Path(str(files(__name__))) / "resources"
    monitor = await asyncio.to_thread(
        (resources / "templates" / "computers" / "icons" / "monitor.svg").read_text,
        encoding="utf-8",
    )
    async with (
        ctx.accept(SERVER_SCHEMA).register(MODULE),
        asyncio.TaskGroup() as tasks,
    ):
        calls = Calls(relay)
        hosts = Hosts(calls, tasks)
        store = Store(ctx.accept(SERVER_DB), access)
        starlink = Starlink(
            store, relay, hosts, lambda reported: ctx.announce(REPORTED, reported)
        )
        pages = await server.pages("computers", __name__)
        views = Views(server, pages, access, store, relay, hosts)
        async with (
            server.module(
                "computers",
                ui=pages.application(views.routes),
                static=resources / "static",
                locales=resources / "locales",
            ),
            server.permission(
                PermissionItem(point=VIEW, description="computers.permission.view")
            ),
            server.permission(
                PermissionItem(point=CREATE, description="computers.permission.create")
            ),
            server.permission(
                PermissionItem(point=UPDATE, description="computers.permission.update")
            ),
            server.permission(
                PermissionItem(point=DELETE, description="computers.permission.delete")
            ),
            server.permission(
                PermissionItem(point=SHARE, description="computers.permission.share")
            ),
            server.sidebar(
                SidebarItem(
                    text="computers.title",
                    icon=monitor,
                    path="/computers",
                    group="server.sidebar.workbench",
                    permission=VIEW,
                    order=30,
                    badge="computers.count",
                )
            ),
            server.topic(
                Topic(name="computers.count", fetch=views.counted, permission=VIEW)
            ),
            server.topic(
                Topic(name="computers.list", fetch=views.listed, permission=VIEW)
            ),
            server.topic(
                Topic(
                    name="computers.computer",
                    fetch=views.computer,
                    fields=("computer",),
                    permission=VIEW,
                )
            ),
            access.shared(
                Shared(
                    kind="computer",
                    permission=SHARE,
                    points=(VIEW, UPDATE, DELETE),
                    name=views.name,
                )
            ),
            ctx.accept(OVERVIEW).register(
                OverviewItem(
                    key="computers",
                    title="computers.stalls",
                    src="/ui/computers/stall",
                    permission=VIEW,
                    columns=2,
                )
            ),
            http.route("/starlink/v1/listen", starlink.listen, methods=["GET"]),
            http.route(
                "/starlink/v1/announce/{number:int}",
                starlink.announce,
                methods=["PUT"],
            ),
            http.route("/starlink/v1/report", starlink.report, methods=["POST"]),
        ):
            ctx.offer(COMPUTERS, calls)
            try:
                yield
            finally:
                # held listens and questions out end before the routes wait
                # for their requests
                starlink.stop()
                hosts.close()


@asynccontextmanager
async def _relay(ctx: Context) -> AsyncIterator[None]:
    relay = MemoryRelay(ctx.accept(TIMER))
    ctx.offer(RELAY, relay)
    try:
        yield
    finally:
        await relay.close()


computer = Contrib(
    accepts=[
        SERVER,
        ACCESS,
        HTTP,
        RELAY,
        OVERVIEW,
        SERVER_DB,
        SERVER_SCHEMA,
        SERVER_MIGRATIONS,
    ],
    offers=[COMPUTERS],
    lifespan=_computer,
)
relay = Contrib(offers=[RELAY], accepts=[TIMER], lifespan=_relay)
