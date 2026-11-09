"""The server's entries as installed, server-computer among them, with the
door as a way in for people other than the admin, and a module that calls
the computers, hears what they report and when they leave, and shows a
computer's ref in its stall."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
lazy import asyncio
lazy import json
lazy import logging
lazy import re
lazy from collections.abc import AsyncIterator
lazy from importlib.resources import files
lazy from pathlib import Path
lazy from typing import Any

import httpx
from bazaar_contrib_http import HTTP
from bazaar_contrib_server import SERVER
from bazaar_contrib_server_access import ACCESS
from bazaar_contrib_server_database import SERVER_DB
from bazaar_contrib_sidecar import RemoteError
from bazaar_core import Context, Contrib, ContribEntry, ContribHub, ContribSpec, load
from door import Door
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from bazaar_contrib_server_computer import (
    COMPUTER_LEFT,
    COMPUTER_REMOVED,
    COMPUTERS,
    REPORTED,
    ComputerLeft,
    ComputerRemoved,
    Reported,
)

ENTRIES = (
    ("timer-wheel", {}),
    ("http", {"listen": "127.0.0.1:0"}),
    ("server-database", {}),
    ("server-migrations", {}),
    ("server", {}),
    ("server-theme-night", {}),
    ("server-theme-day", {}),
    ("server-access", {}),
    ("server-overview", {}),
    ("server-relay-memory", {}),
    ("server-computer", {}),
)


@dataclass
class Served:
    hub: ContribHub
    url: str
    password: str
    door: Door
    # what the computers reported, as it was announced
    reported: list[Reported] = field(default_factory=list)
    # the computers removed, as it was announced
    removed: list[ComputerRemoved] = field(default_factory=list)
    # the computers said to have left, as it was announced
    left: list[ComputerLeft] = field(default_factory=list)


@asynccontextmanager
async def serving() -> AsyncIterator[Served]:
    found: dict[str, Any] = {}
    reported: list[Reported] = []
    removed: list[ComputerRemoved] = []
    left: list[ComputerLeft] = []
    printed: list[str] = []

    class Printed(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            printed.append(record.getMessage())

    @asynccontextmanager
    async def look(ctx: Context) -> AsyncIterator[None]:
        found["url"] = ctx.accept(HTTP).url
        yield

    @asynccontextmanager
    async def enter(ctx: Context) -> AsyncIterator[None]:
        server = ctx.accept(SERVER)
        pages = await server.pages("door", "door")
        locales = (
            (await asyncio.to_thread(lambda: Path(str(files("door")))))
            / "resources"
            / "locales"
        )

        async def settings(request: Request) -> Response:
            return await pages.render(request, "door/settings.html")

        found["door"] = Door(ctx.accept(ACCESS), ctx.accept(SERVER_DB))
        async with (
            server.module(
                "door", ui=pages.application([Route("/", settings)]), locales=locales
            ),
            ctx.accept(ACCESS).method(found["door"]),
        ):
            yield

    @asynccontextmanager
    async def probe(ctx: Context) -> AsyncIterator[None]:
        """Calls a computer what a page would: POST /ui/probe/<ref>/<method>
        with the arguments as a JSON list."""
        server = ctx.accept(SERVER)
        computers = ctx.accept(COMPUTERS)

        async def ask(request: Request) -> Response:
            ids = await server.refs.values([request.path_params["ref"]])
            assert ids is not None
            try:
                value = await computers.call(
                    ids[0],
                    "probe",
                    request.path_params["method"],
                    *json.loads(await request.body()),
                )
            except TimeoutError:
                return JSONResponse({"silent": True}, status_code=504)
            except RemoteError as error:
                return JSONResponse({"failed": error.type}, status_code=502)
            return JSONResponse({"value": value})

        async def goods(request: Request) -> Response:
            # what the stall shows under a computer's name: the ref it was given
            return HTMLResponse(
                '<!DOCTYPE html><html><head><script src="/static/server/embed.js">'
                f'</script></head><body><p id="goods">{request.path_params["ref"]}'
                "</p></body></html>"
            )

        async def listing(request: Request) -> Response:
            ids = request.query_params.getlist("id")
            return JSONResponse(
                [
                    {"id": item.id, "name": item.name, "online": item.online}
                    for item in await computers.computers(ids or None)
                ]
            )

        async with (
            server.module(
                "probe",
                ui=Starlette(
                    routes=[
                        Route("/computers", listing),
                        Route("/stall/{ref}", goods),
                        Route("/{ref}/{method}", ask, methods=["POST"]),
                    ]
                ),
            ),
            computers.stall("/ui/probe/stall"),
        ):
            yield

    async def heard(ctx: Context, value: Reported) -> None:
        del ctx
        reported.append(value)

    async def gone(ctx: Context, value: ComputerRemoved) -> None:
        del ctx
        removed.append(value)

    async def away(ctx: Context, value: ComputerLeft) -> None:
        del ctx
        left.append(value)

    composition = await asyncio.to_thread(
        load,
        [ContribSpec(contrib=name, config=config) for name, config in ENTRIES],
    )
    composition = composition.model_copy(
        update={
            "entries": (
                *composition.entries,
                ContribEntry(
                    name="door",
                    definition=Contrib(
                        accepts=[SERVER, ACCESS, SERVER_DB], lifespan=enter
                    ),
                ),
                ContribEntry(
                    name="probe",
                    definition=Contrib(
                        accepts=[SERVER, COMPUTERS],
                        lifespan=probe,
                        listen={
                            REPORTED: heard,
                            COMPUTER_REMOVED: gone,
                            COMPUTER_LEFT: away,
                        },
                    ),
                ),
                ContribEntry(
                    name="look", definition=Contrib(accepts=[HTTP], lifespan=look)
                ),
            )
        }
    )
    logger = logging.getLogger("bazaar_contrib_server_access")
    handler = Printed()
    logger.addHandler(handler)
    try:
        async with ContribHub() as hub, hub.enable(composition):
            password = next(
                (
                    created.group(1)
                    for message in printed
                    if (created := re.search(r"with password (\S+);", message))
                ),
                "",
            )
            yield Served(
                hub, found["url"], password, found["door"], reported, removed, left
            )
    finally:
        logger.removeHandler(handler)


@asynccontextmanager
async def client() -> AsyncIterator[httpx.AsyncClient]:
    # building the client loads certificates from disk, which is not the loop's work
    async with await asyncio.to_thread(httpx.AsyncClient, timeout=90) as session:
        yield session


async def admin(served: Served, session: httpx.AsyncClient) -> None:
    await session.post(
        f"{served.url}/login/", data={"name": "admin", "password": served.password}
    )


async def role(served: Served, session: httpx.AsyncClient, *points: str) -> str:
    """A new role holding the points; the address of its page."""
    made = await session.post(
        f"{served.url}/ui/access/roles/",
        data={"name": "holding-" + "-".join(points), "permissions": list(points)},
    )
    assert made.status_code == 303
    return made.headers["location"]


async def through(served: Served, session: httpx.AsyncClient, person: str) -> None:
    """Log in through the door as the person."""
    sent = await session.get(f"{served.url}/login/door/?person={person}")
    while (
        sent.status_code in (302, 303, 307)
        and "/login/door/" in sent.headers["location"]
    ):
        sent = await session.get(f"{served.url}{sent.headers['location']}")


async def enrol(
    served: Served, session: httpx.AsyncClient, name: str
) -> tuple[str, str]:
    """A computer the account connects; its ref and its token, as the page
    showing the commands gives them."""
    page = await session.post(f"{served.url}/ui/computers/", data={"name": name})
    assert page.status_code == 200, (page.status_code, page.text[:3000])
    token = re.search(r"--option token=([^\"&\s]+)\"", page.text)
    ref = re.search(r'hx-get="/ui/computers/(\d+)"', page.text)
    assert token is not None and ref is not None
    return ref.group(1), token.group(1)


@asynccontextmanager
async def node(served: Served, token: str) -> AsyncIterator[httpx.AsyncClient]:
    """A computer's side of the protocol, with its token."""
    async with await asyncio.to_thread(
        lambda: httpx.AsyncClient(
            base_url=f"{served.url}/starlink/v1",
            headers={"Authorization": f"Bearer {token}"},
            timeout=90,
        )
    ) as session:
        yield session
