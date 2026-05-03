from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

_COOKIE_NAME = "devseek_bridge_session"
_PUBLIC_PATHS = {
    "/",
    "/manifest.webmanifest",
    "/service-worker.js",
    "/api/auth/status",
    "/api/auth/login",
    "/favicon.ico",
}
_PUBLIC_PREFIXES = ("/mobile/",)


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii"))


@dataclass(slots=True)
class BridgeAuth:
    username: str | None
    password: str | None
    secret: str
    cookie_name: str = _COOKIE_NAME
    cookie_max_age: int = 60 * 60 * 24 * 14
    secure_cookie: bool = False

    @classmethod
    def from_inputs(
        cls,
        username: str | None = None,
        password: str | None = None,
        secret: str | None = None,
        secure_cookie: bool = False,
    ) -> "BridgeAuth":
        clean_user = (username or "").strip() or None
        clean_password = password or None
        if bool(clean_user) != bool(clean_password):
            raise ValueError("Configure usuario e senha juntos para ativar a autenticacao.")
        return cls(
            username=clean_user,
            password=clean_password,
            secret=secret or secrets.token_urlsafe(32),
            secure_cookie=secure_cookie,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.username and self.password)

    def _sign(self, payload: str) -> str:
        return hmac.new(
            self.secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def create_session_token(self) -> str:
        payload = {
            "u": self.username,
            "exp": int(time.time()) + self.cookie_max_age,
        }
        encoded = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        return f"{encoded}.{self._sign(encoded)}"

    def verify_session_token(self, token: str | None) -> bool:
        if not self.enabled or not token or "." not in token:
            return False

        encoded, signature = token.rsplit(".", 1)
        expected = self._sign(encoded)
        if not hmac.compare_digest(signature, expected):
            return False

        try:
            payload = json.loads(_b64_decode(encoded).decode("utf-8"))
        except Exception:
            return False

        if payload.get("u") != self.username:
            return False
        if int(payload.get("exp", 0)) < int(time.time()):
            return False
        return True

    def authenticate(self, username: str, password: str) -> bool:
        if not self.enabled:
            return True
        return hmac.compare_digest(username or "", self.username or "") and hmac.compare_digest(
            password or "",
            self.password or "",
        )

    def is_authenticated(self, request: Request) -> bool:
        return self.verify_session_token(request.cookies.get(self.cookie_name))

    def apply_login_cookie(self, response: Response) -> None:
        response.set_cookie(
            self.cookie_name,
            self.create_session_token(),
            max_age=self.cookie_max_age,
            httponly=True,
            samesite="lax",
            secure=self.secure_cookie,
            path="/",
        )

    def clear_login_cookie(self, response: Response) -> None:
        response.delete_cookie(self.cookie_name, path="/")

    def status_payload(self, request: Request) -> dict[str, bool]:
        return {
            "enabled": self.enabled,
            "authenticated": self.is_authenticated(request),
        }


class BridgeAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, auth: BridgeAuth):
        super().__init__(app)
        self.auth = auth

    async def dispatch(self, request: Request, call_next):
        if not self.auth.enabled or self._is_public_path(request.url.path):
            return await call_next(request)

        if self.auth.is_authenticated(request):
            return await call_next(request)

        if request.url.path.startswith("/api/") or request.url.path.startswith("/preview/"):
            return JSONResponse({"detail": "Autenticacao necessaria."}, status_code=401)

        return await call_next(request)

    @staticmethod
    def _is_public_path(path: str) -> bool:
        return path in _PUBLIC_PATHS or any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)
