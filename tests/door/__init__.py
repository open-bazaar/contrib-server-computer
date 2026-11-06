"""A login method for the tests: a site whose people are whoever the link
names, so a login can set off, come back and be renewed on this machine."""

from __future__ import annotations

from dataclasses import dataclass, field
lazy from collections.abc import Mapping
lazy from urllib.parse import urlencode

from bazaar_contrib_server_access import Access, Authorization, Confirmed, Refused
from bazaar_contrib_storage import Database, Session
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

ICON = (
    '<svg viewBox="0 0 24 24"><rect width="24" height="24" rx="6.5" fill="#4d5fd6"/>'
    '<circle cx="12" cy="12" r="5" fill="#fff"/></svg>'
)


@dataclass
class Door:
    access: Access
    database: Database
    # the role the door gives first logins, if any
    role: str | None = None
    # who came through it, by name, and whose authorization was taken back
    people: dict[str, str] = field(default_factory=dict)
    revoked: set[str] = field(default_factory=set)

    app: ASGIApp = field(init=False)

    key = "door"
    name = "door.name"
    description = "door.description"
    icon = ICON
    settings = "/ui/door/"

    def __post_init__(self) -> None:
        self.app = Starlette(
            routes=[
                Route("/", self._start),
                Route("/approve", self._approve),
                Route("/callback", self._callback),
            ]
        )

    async def ready(self) -> bool:
        return True

    async def roles(self) -> set[str]:
        return {self.role} if self.role else set()

    async def renew(self, account_id: str) -> Confirmed | None:
        if account_id in self.revoked:
            return None
        name = next(name for name, held in self.people.items() if held == account_id)
        return Confirmed(account_id, name.title())

    async def _start(self, request: Request) -> Response:
        person = request.query_params.get("person", "")

        async def authorize(authorization: Authorization) -> str:
            return "/login/door/approve?" + urlencode(
                {"state": authorization.state, "person": person}
            )

        return await self.access.start(request, self.key, authorize)

    async def _approve(self, request: Request) -> Response:
        person = request.query_params["person"]
        answer = {"state": request.query_params["state"]}
        # nobody named declines
        answer |= {"code": person} if person else {"error": "access_denied"}
        return RedirectResponse("/login/door/callback?" + urlencode(answer))

    async def _callback(self, request: Request) -> Response:
        return await self.access.finish(request, self.key, self._confirm)

    async def _confirm(
        self, authorization: Authorization, query: Mapping[str, str]
    ) -> Confirmed:
        del authorization
        person = query.get("code", "")
        if not person:
            raise Refused
        if person not in self.people:

            async def enroll(session: Session) -> str:
                return await self.access.enroll(session, self.key, self.role)

            self.people[person] = await self.database.transaction(enroll)
        return Confirmed(self.people[person], person.title())
