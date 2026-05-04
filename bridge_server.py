from __future__ import annotations

import argparse
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

from core.mobile_auth import BridgeAuth, BridgeAuthMiddleware
from core.mobile_bridge import BridgeService

WEB_ROOT = Path(__file__).resolve().parent / "web" / "mobile"


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    include_structure: bool = True
    include_instructions: bool = True
    include_search: bool = True
    deep_think: bool = False
    pensamento_profundo: bool = False
    web_search: bool = False
    auto_apply: bool = True
    execute_run_commands: bool = False


class ApplyJobRequest(BaseModel):
    execute_run_commands: bool = False


class WriteFileRequest(BaseModel):
    path: str = Field(..., min_length=1)
    content: str = ""


class TerminalRunRequest(BaseModel):
    command: str = Field(..., min_length=1)
    timeout_seconds: int = Field(default=60, ge=1, le=3600)


class OpenProjectRequest(BaseModel):
    path: str = Field(..., min_length=1)


class CreateProjectRequest(BaseModel):
    parent_path: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)


class AuthLoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


def create_app(
    project_path: str,
    auth_user: str | None = None,
    auth_password: str | None = None,
    auth_secret: str | None = None,
    secure_cookie: bool = False,
) -> FastAPI:
    service = BridgeService(project_path)
    auth = BridgeAuth.from_inputs(
        username=auth_user,
        password=auth_password,
        secret=auth_secret,
        secure_cookie=secure_cookie,
    )
    app = FastAPI(title="DevSeek Mobile Bridge")
    app.state.service = service
    app.state.auth = auth
    app.add_middleware(BridgeAuthMiddleware, auth=auth)
    app.mount("/mobile", StaticFiles(directory=str(WEB_ROOT), html=True), name="mobile")

    @app.on_event("shutdown")
    def _shutdown() -> None:
        service.close()

    @app.get("/")
    def root():
        return FileResponse(WEB_ROOT / "index.html")

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(
            WEB_ROOT / "manifest.webmanifest",
            media_type="application/manifest+json",
        )

    @app.get("/service-worker.js")
    def service_worker():
        return FileResponse(
            WEB_ROOT / "service-worker.js",
            media_type="application/javascript",
        )

    @app.get("/api/auth/status")
    def api_auth_status(request: Request):
        return auth.status_payload(request)

    @app.post("/api/auth/login")
    def api_auth_login(payload: AuthLoginRequest, response: Response):
        if not auth.enabled:
            return {"enabled": False, "authenticated": True}
        if not auth.authenticate(payload.username, payload.password):
            raise HTTPException(status_code=401, detail="Usuario ou senha invalidos.")
        auth.apply_login_cookie(response)
        return {"enabled": True, "authenticated": True}

    @app.post("/api/auth/logout")
    def api_auth_logout(response: Response):
        auth.clear_login_cookie(response)
        return {"authenticated": False}

    @app.get("/api/state")
    def api_state():
        return service.get_state()

    @app.post("/api/sessions/new")
    def api_new_session():
        return service.new_session()

    @app.get("/api/projects/browser")
    def api_project_browser(path: str | None = None):
        try:
            return service.browse_projects(path)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/open")
    def api_open_project(payload: OpenProjectRequest):
        try:
            return service.open_project(payload.path)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/create")
    def api_create_project(payload: CreateProjectRequest):
        try:
            return service.create_project(payload.parent_path, payload.name)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/deepseek/status")
    def api_deepseek_status():
        connected, message = service.get_connection_status()
        return {"connected": connected, "message": message}

    @app.post("/api/deepseek/open-browser")
    def api_deepseek_open_browser():
        try:
            connected, message = service.open_login_browser()
            return {"connected": connected, "message": message}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/chat")
    def api_chat(payload: ChatRequest):
        try:
            return service.submit_chat(
                payload.message,
                include_structure=payload.include_structure,
                include_instructions=payload.include_instructions,
                include_search=payload.include_search,
                deep_think=payload.deep_think,
                pensamento_profundo=payload.pensamento_profundo,
                web_search=payload.web_search,
                auto_apply=payload.auto_apply,
                execute_run_commands=payload.execute_run_commands,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: str):
        try:
            return service.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job nao encontrado.") from exc

    @app.post("/api/jobs/{job_id}/apply")
    def api_apply_job(job_id: str, payload: ApplyJobRequest):
        try:
            return service.apply_job(job_id, execute_run_commands=payload.execute_run_commands)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job nao encontrado.") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/files/tree")
    def api_file_tree():
        return {"items": service.list_files()}

    @app.get("/api/files/content")
    def api_file_content(path: str):
        try:
            return service.read_file(path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/files/content")
    def api_write_file(payload: WriteFileRequest):
        try:
            return service.write_file(payload.path, payload.content)
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/terminal/run")
    def api_terminal_run(payload: TerminalRunRequest):
        try:
            return service.run_terminal_command(payload.command, payload.timeout_seconds)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/preview/status")
    def api_preview_status():
        return service.get_preview_state()

    @app.post("/api/preview/start")
    def api_preview_start():
        try:
            return service.start_preview_server()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/preview/stop")
    def api_preview_stop():
        return service.stop_preview_server()

    @app.get("/preview/")
    @app.get("/preview/{asset_path:path}")
    def preview(asset_path: str = ""):
        try:
            return FileResponse(service.resolve_preview_path(asset_path))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the DevSeek mobile bridge.")
    parser.add_argument("--project", default=".", help="Project directory exposed to the mobile IDE.")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind the HTTP server.")
    parser.add_argument("--port", type=int, default=5000, help="Port to bind the HTTP server.")
    parser.add_argument(
        "--auth-user",
        default=os.getenv("DEVSEEK_BRIDGE_AUTH_USER", ""),
        help="Optional login username for protecting the mobile bridge.",
    )
    parser.add_argument(
        "--auth-password",
        default=os.getenv("DEVSEEK_BRIDGE_AUTH_PASSWORD", ""),
        help="Optional login password for protecting the mobile bridge.",
    )
    parser.add_argument(
        "--auth-secret",
        default=os.getenv("DEVSEEK_BRIDGE_AUTH_SECRET", ""),
        help="Optional cookie signing secret. If omitted, a temporary secret is generated on startup.",
    )
    parser.add_argument(
        "--secure-cookie",
        action="store_true",
        default=os.getenv("DEVSEEK_BRIDGE_SECURE_COOKIE", "").strip().lower() in {"1", "true", "yes", "on"},
        help="Mark auth cookies as Secure. Use this when the bridge is reached only over HTTPS.",
    )
    args = parser.parse_args()

    app = create_app(
        args.project,
        auth_user=args.auth_user,
        auth_password=args.auth_password,
        auth_secret=args.auth_secret,
        secure_cookie=args.secure_cookie,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
