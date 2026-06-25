from typing import Any, Mapping
import logging

from starlette.requests import HTTPConnection
from starlette.responses import PlainTextResponse
from starlette.types import Receive, Scope, Send

from ..errors import IapInvalidJwtError, IapMissingEmailInJwtError
from ..authentication_backend import IapAuthenticationBackend
from ..base import IAPAuthenticationMiddleware

logger = logging.getLogger(__name__)


class StarletteAzureAuthenticationMiddleware(IAPAuthenticationMiddleware):
    """A middleware to verify Azure AD JWT assertions for a Starlette-powered application.

    The email of the user is expected to be included in the token payload.
    """

    PUBLIC_PATHS = frozenset({"/health"})

    def __init__(self, app: Any, backend: IapAuthenticationBackend) -> None:
        super().__init__(backend)
        self.app = app

    def save_information(self, scope: Scope, data: Mapping[str, Any]) -> None:
        # Store the stable identity and display claims in the ASGI scope for later use:
        scope["oid"] = data.get("oid")
        scope["preferred_username"] = data.get("preferred_username")
        scope["name"] = data.get("name")

    def on_error(self) -> PlainTextResponse:
        return PlainTextResponse(content="Invalid JWT", status_code=403)

    def on_missing_header(self) -> PlainTextResponse:
        return PlainTextResponse(
            content="Missing or invalid Authorization header", status_code=403
        )

    def on_invalid_jwt(self) -> PlainTextResponse:
        return PlainTextResponse(content="Invalid JWT", status_code=403)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> Any:
        # Only handle HTP requests here
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope["path"] in self.PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        conn = HTTPConnection(scope)
        auth_header = conn.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            resp = self.on_missing_header()
            await resp(scope, receive, send)
            return

        token = auth_header.split(" ", 1)[1].strip()
        try:
            data = self.backend.decode(token)
            email = data.get("preferred_username")
            if email is None:
                raise IapMissingEmailInJwtError

            self.save_information(scope, data)

        except IapInvalidJwtError as e:
            logger.exception(e)
            resp = self.on_invalid_jwt()
            await resp(scope, receive, send)
        except IapMissingEmailInJwtError as e:
            logger.exception(e)
            resp = self.on_error()
            await resp(scope, receive, send)
        except Exception as e:
            logger.exception(e)
            resp - self.on_error()
            await resp(scope, receive, send)
        else:
            # Token is valid-dispatch to the app
            await self.app(scope, receive, send)


FastAPIAzureAuthenticationMiddleware = StarletteAzureAuthenticationMiddleware
