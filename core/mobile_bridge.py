from __future__ import annotations

import json
import os
import shutil
import string
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from core.command_parser import apply_command, extract_chat_text, parse_commands
from core.context_manager import ContextManager
from core.deepseek_bot import (
    FINISH_MARKER,
    check_deepseek_status_sync,
    get_browser_choice,
    open_login_browser_sync,
    send_prompt_sync,
)
from core.file_searcher import FileSearcher
from core.history_manager import HistoryManager

_BLOCKED_SEGMENTS = {".devseek", ".git", "node_modules", "__pycache__", ".venv", "venv"}
_SYSTEM_COLOR = "#F0B429"
_USER_COLOR = "#4FC1FF"
_ASSISTANT_COLOR = "#3DD68C"
_RECENT_PROJECT_LIMIT = 8
_FINISH_INSTRUCTION = (
    "\n\n---\n"
    "**SYSTEM INSTRUCTION (DevSeek):** At the end of the response, write exactly "
    f"`{FINISH_MARKER}` on the last line and nothing after it."
)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean_relative_path(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("/")


@dataclass
class BridgeJob:
    job_id: str
    session_id: str
    message: str
    options: dict[str, Any]
    created_at: str = field(default_factory=_now_iso)
    status: str = "queued"
    status_message: str = "Na fila."
    events: list[str] = field(default_factory=list)
    raw_response: str = ""
    chat_text: str = ""
    error: str = ""
    commands: list[dict[str, Any]] = field(default_factory=list)
    apply_results: list[dict[str, Any]] = field(default_factory=list)
    apply_status: str = "not_requested"
    finished_at: str | None = None

    def add_event(self, message: str) -> None:
        self.status_message = message
        self.events.append(message)
        if len(self.events) > 80:
            self.events = self.events[-80:]

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "session_id": self.session_id,
            "message": self.message,
            "status": self.status,
            "status_message": self.status_message,
            "events": list(self.events),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "chat_text": self.chat_text,
            "error": self.error,
            "command_count": len(self.commands),
            "commands": list(self.commands),
            "apply_status": self.apply_status,
            "apply_results": list(self.apply_results),
            "options": dict(self.options),
        }


class BridgeService:
    def __init__(
        self,
        project_path: str,
        send_prompt_fn: Callable[..., str] | None = None,
    ):
        self.send_prompt_fn = send_prompt_fn or send_prompt_sync
        self._jobs: dict[str, BridgeJob] = {}
        self._lock = threading.RLock()
        self._recent_projects: list[str] = []

        self.project_path = Path(project_path).resolve()
        self.context_manager = ContextManager(str(self.project_path))
        self.history_manager: HistoryManager | None = None

        self._preview_process: subprocess.Popen | None = None
        self._preview_logs: list[str] = []
        self._preview_status = "idle"
        self._preview_command = ""
        self._preview_framework = ""
        self._preview_package_manager = ""
        self._preview_port: int | None = None
        self._preview_error = ""
        self._preview_started_at: str | None = None
        self._preview_exit_code: int | None = None

        self._set_project(self.project_path, allow_running_jobs=True)

    def get_state(self) -> dict[str, Any]:
        connected, message = self.get_connection_status()
        with self._lock:
            preview = self.get_preview_state()
            messages = [
                self._serialize_message(msg)
                for msg in self.history_manager.get_current_messages()
            ]
            jobs = sorted(
                (job.snapshot() for job in self._jobs.values()),
                key=lambda item: item["created_at"],
                reverse=True,
            )[:12]
            return {
                "project_name": self.project_path.name,
                "project_path": str(self.project_path),
                "session_id": self.history_manager.current_session_id,
                "preview_url": preview["iframe_url"],
                "preview_ready": preview["ready"],
                "preview": preview,
                "browser": get_browser_choice(),
                "deepseek": {"connected": connected, "message": message},
                "messages": messages,
                "jobs": jobs,
                "recent_projects": list(self._recent_projects),
            }

    def new_session(self) -> dict[str, Any]:
        with self._lock:
            session_id = self.history_manager.new_session()
            return {"session_id": session_id}

    def close(self) -> None:
        self.stop_preview_server()

    def get_connection_status(self) -> tuple[bool, str]:
        return check_deepseek_status_sync()

    def open_login_browser(self) -> tuple[bool, str]:
        return open_login_browser_sync()

    def browse_projects(self, path: str | None = None) -> dict[str, Any]:
        with self._lock:
            current = self._resolve_browser_path(path)
            try:
                parent = str(current.parent) if current.parent != current else None
            except Exception:
                parent = None

            items = []
            try:
                children = sorted(
                    (entry for entry in current.iterdir() if entry.is_dir()),
                    key=lambda entry: entry.name.lower(),
                )
            except Exception as exc:
                raise RuntimeError(f"Nao foi possivel listar {current}: {exc}") from exc

            for child in children:
                if child.name in _BLOCKED_SEGMENTS or child.name.startswith("."):
                    continue
                items.append({
                    "name": child.name,
                    "path": str(child),
                    "is_current": child.resolve() == self.project_path,
                    "is_project_hint": self._is_project_hint(child),
                    "has_package_json": (child / "package.json").exists(),
                    "has_index_html": (child / "index.html").exists(),
                })

            return {
                "current_path": str(current),
                "parent_path": parent,
                "current_project": str(self.project_path),
                "roots": self._list_browser_roots(),
                "items": items,
            }

    def open_project(self, path: str) -> dict[str, Any]:
        target = self._resolve_browser_path(path)
        with self._lock:
            self._set_project(target)
        return self.get_state()

    def create_project(self, parent_path: str, name: str) -> dict[str, Any]:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("Informe um nome para a pasta do projeto.")
        if any(sep in clean_name for sep in ("/", "\\")) or clean_name in {".", ".."}:
            raise ValueError("Nome de pasta invalido.")

        parent = self._resolve_browser_path(parent_path)
        target = parent / clean_name
        if target.exists():
            raise ValueError("Ja existe uma pasta com esse nome.")

        target.mkdir(parents=True, exist_ok=False)
        with self._lock:
            self._set_project(target)
        return self.get_state()

    def get_preview_state(self) -> dict[str, Any]:
        with self._lock:
            static_available = (self.project_path / "index.html").exists()
            strategy = self._detect_preview_strategy()
            running = self._preview_process is not None and self._preview_process.poll() is None

            if self._preview_process is not None and not running:
                self._preview_exit_code = self._preview_process.poll()
                if self._preview_status == "running":
                    self._preview_status = "stopped"
                self._preview_process = None

            mode = "none"
            iframe_url = "/preview/"
            ready = False
            if running and self._preview_port:
                mode = "dev_server"
                ready = True
            elif strategy["mode"] == "dev_server":
                mode = "none"
                ready = False
            elif static_available:
                mode = "static"
                ready = True

            return {
                "mode": mode,
                "ready": ready,
                "static_available": static_available,
                "iframe_url": iframe_url,
                "suggested_mode": strategy["mode"],
                "suggested_reason": strategy["reason"],
                "dev_server": {
                    "status": self._preview_status,
                    "framework": self._preview_framework,
                    "package_manager": self._preview_package_manager,
                    "command": self._preview_command,
                    "port": self._preview_port,
                    "started_at": self._preview_started_at,
                    "exit_code": self._preview_exit_code,
                    "error": self._preview_error,
                    "log_tail": self._preview_logs[-30:],
                    "can_start": strategy["can_start"],
                    "recommended_command": strategy.get("display_command", ""),
                    "recommended_port": strategy.get("default_port"),
                    "detected_framework": strategy.get("framework", ""),
                },
            }

    def start_preview_server(self) -> dict[str, Any]:
        with self._lock:
            if self._preview_process is not None and self._preview_process.poll() is None:
                return self.get_preview_state()

            strategy = self._detect_preview_strategy()
            if not strategy["can_start"]:
                raise RuntimeError(strategy["reason"])

            argv = strategy["argv"]
            env = {**os.environ, **strategy.get("env", {})}
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

            self._preview_logs = []
            self._preview_status = "starting"
            self._preview_command = strategy["display_command"]
            self._preview_framework = strategy.get("framework", "")
            self._preview_package_manager = strategy.get("package_manager", "")
            self._preview_port = strategy.get("default_port")
            self._preview_error = ""
            self._preview_started_at = _now_iso()
            self._preview_exit_code = None

            try:
                self._preview_process = subprocess.Popen(
                    argv,
                    cwd=str(self.project_path),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    creationflags=creationflags,
                )
            except FileNotFoundError as exc:
                self._preview_process = None
                self._preview_status = "error"
                self._preview_error = f"Comando nao encontrado: {argv[0]}"
                raise RuntimeError(self._preview_error) from exc
            except Exception as exc:
                self._preview_process = None
                self._preview_status = "error"
                self._preview_error = str(exc)
                raise RuntimeError(f"Nao foi possivel iniciar o preview: {exc}") from exc

            self._append_preview_log(f"$ {self._preview_command}")
            for pipe_name, pipe in (("stdout", self._preview_process.stdout), ("stderr", self._preview_process.stderr)):
                if pipe is None:
                    continue
                threading.Thread(
                    target=self._consume_preview_pipe,
                    args=(pipe_name, pipe),
                    daemon=True,
                ).start()
            return self.get_preview_state()

    def stop_preview_server(self) -> dict[str, Any]:
        with self._lock:
            proc = self._preview_process
            if proc is None:
                self._preview_status = "idle"
                return self.get_preview_state()

            try:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            finally:
                self._preview_exit_code = proc.poll()
                self._preview_process = None
                self._preview_status = "idle"
                self._append_preview_log("Preview finalizado.")

            return self.get_preview_state()

    def list_files(self) -> list[dict[str, Any]]:
        return self._build_tree(self.project_path)

    def read_file(self, path: str) -> dict[str, Any]:
        target, rel_path = self._resolve_project_path(path)
        if target.is_dir():
            raise ValueError(f"'{rel_path}' e um diretorio.")
        if not target.exists():
            raise FileNotFoundError(rel_path)
        return {
            "path": rel_path,
            "content": target.read_text(encoding="utf-8", errors="replace"),
        }

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        target, rel_path = self._resolve_project_path(path)
        backup_dir = self.context_manager.devseek_path / "backups"
        if target.exists():
            self._backup_file(target, backup_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.context_manager.update_structure()
        return {"path": rel_path, "saved": True}

    def resolve_preview_path(self, path: str = "") -> Path:
        rel_path = _clean_relative_path(path)
        if not rel_path:
            target = self.project_path / "index.html"
            if not target.exists():
                raise FileNotFoundError("index.html")
            return target

        target, _ = self._resolve_project_path(rel_path)
        if target.is_dir():
            index_path = target / "index.html"
            if index_path.exists():
                return index_path
            raise FileNotFoundError(rel_path)
        if not target.exists():
            raise FileNotFoundError(rel_path)
        return target

    def submit_chat(
        self,
        message: str,
        *,
        include_structure: bool = True,
        include_instructions: bool = True,
        include_search: bool = True,
        deep_think: bool = False,
        pensamento_profundo: bool = False,
        web_search: bool = False,
        auto_apply: bool = True,
        execute_run_commands: bool = False,
    ) -> dict[str, Any]:
        clean_message = message.strip()
        if not clean_message:
            raise ValueError("A mensagem nao pode ficar vazia.")

        with self._lock:
            session_id = self.history_manager.current_session_id or self.history_manager.new_session()
            job = BridgeJob(
                job_id=uuid.uuid4().hex,
                session_id=session_id,
                message=clean_message,
                options={
                    "include_structure": include_structure,
                    "include_instructions": include_instructions,
                    "include_search": include_search,
                    "deep_think": deep_think,
                    "pensamento_profundo": pensamento_profundo,
                    "web_search": web_search,
                    "auto_apply": auto_apply,
                    "execute_run_commands": execute_run_commands,
                },
            )
            job.add_event("Aguardando envio ao DeepSeek...")
            self._jobs[job.job_id] = job
            self.history_manager.add_message("Voce", clean_message, _USER_COLOR, session_id=session_id)

        worker = threading.Thread(
            target=self._run_job,
            args=(job.job_id,),
            name=f"bridge-job-{job.job_id[:8]}",
            daemon=True,
        )
        worker.start()
        return job.snapshot()

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return job.snapshot()

    def apply_job(self, job_id: str, execute_run_commands: bool = False) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status != "completed":
                raise RuntimeError("A resposta ainda nao terminou.")
            if job.apply_status == "applied":
                return job.snapshot()
            if job.apply_status == "applying":
                raise RuntimeError("Este job ja esta sendo aplicado.")
            job.apply_status = "applying"
            raw_response = job.raw_response

        try:
            commands = parse_commands(raw_response)
            results = self._apply_commands(commands, execute_run_commands=execute_run_commands)
        except Exception:
            with self._lock:
                job = self._jobs[job_id]
                job.apply_status = "pending"
            raise

        with self._lock:
            job.apply_results = results
            job.apply_status = "applied"
            self._record_apply_summary(job.session_id, results)
            return job.snapshot()

    def run_terminal_command(self, command: str, timeout_seconds: int = 60) -> dict[str, Any]:
        if not command.strip():
            raise ValueError("Comando vazio.")

        shell_command = ["powershell.exe", "-NoProfile", "-Command", command]
        if shutil.which("powershell.exe") is None:
            shell_command = ["bash", "-lc", command]

        try:
            result = subprocess.run(
                shell_command,
                cwd=str(self.project_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
            return {
                "command": command,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timed_out": False,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "command": command,
                "returncode": None,
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
                "timed_out": True,
            }

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = "running"

        try:
            prompt = self._build_prompt(job.message, job.options)
            response = self.send_prompt_fn(
                prompt,
                deep_think=bool(job.options.get("deep_think")),
                pensamento_profundo=bool(job.options.get("pensamento_profundo")),
                web_search=bool(job.options.get("web_search")),
                status_callback=lambda text: self._update_job_event(job_id, text),
            )
            commands = parse_commands(response)
            chat_text = extract_chat_text(response) or response
            serialized_commands = [self._serialize_command(cmd) for cmd in commands]

            apply_results: list[dict[str, Any]] = []
            apply_status = "pending" if commands else "not_requested"
            if commands and job.options.get("auto_apply"):
                apply_results = self._apply_commands(
                    commands,
                    execute_run_commands=bool(job.options.get("execute_run_commands")),
                )
                apply_status = "applied"

            with self._lock:
                job = self._jobs[job_id]
                job.raw_response = response
                job.chat_text = chat_text
                job.commands = serialized_commands
                job.apply_results = apply_results
                job.apply_status = apply_status
                job.status = "completed"
                job.finished_at = _now_iso()
                job.add_event("Resposta pronta.")
                self.history_manager.add_message("DeepSeek", response, _ASSISTANT_COLOR, session_id=job.session_id)
                if apply_results:
                    self._record_apply_summary(job.session_id, apply_results)
        except Exception as exc:
            with self._lock:
                job = self._jobs[job_id]
                job.status = "failed"
                job.error = str(exc)
                job.finished_at = _now_iso()
                job.add_event(f"Erro: {exc}")
                self.history_manager.add_message("Sistema", str(exc), "#F44747", session_id=job.session_id)

    def _build_prompt(self, message: str, options: dict[str, Any]) -> str:
        relevant_files = []
        if options.get("include_search"):
            relevant_files = FileSearcher(str(self.project_path)).search_relevant_files(message)

        prompt = self.context_manager.build_prompt(
            message,
            relevant_files,
            include_structure=bool(options.get("include_structure", True)),
            include_instructions=bool(options.get("include_instructions", True)),
        )
        return prompt + _FINISH_INSTRUCTION

    def _update_job_event(self, job_id: str, text: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status == "failed":
                return
            job.add_event(text)

    def _set_project(self, path: Path, allow_running_jobs: bool = False) -> None:
        path = path.resolve()
        if not path.exists() or not path.is_dir():
            raise ValueError(f"Pasta invalida: {path}")

        if not allow_running_jobs and any(job.status == "running" for job in self._jobs.values()):
            raise RuntimeError("Existe um job em execucao. Espere terminar antes de trocar de projeto.")

        self.stop_preview_server()
        self.project_path = path
        self.context_manager = ContextManager(str(self.project_path))
        self.context_manager.initialize()
        self.history_manager = HistoryManager(self.context_manager.devseek_path)
        self.history_manager.resume_last_session()
        self._jobs.clear()
        self._remember_recent_project(self.project_path)

    def _remember_recent_project(self, path: Path) -> None:
        raw = str(path.resolve())
        self._recent_projects = [item for item in self._recent_projects if item != raw]
        self._recent_projects.insert(0, raw)
        self._recent_projects = self._recent_projects[:_RECENT_PROJECT_LIMIT]

    def _resolve_browser_path(self, path: str | None) -> Path:
        raw = (path or "").strip()
        if not raw:
            return self.project_path
        target = Path(raw).expanduser().resolve()
        if not target.exists() or not target.is_dir():
            raise ValueError(f"Pasta nao encontrada: {target}")
        return target

    def _list_browser_roots(self) -> list[dict[str, str]]:
        roots: list[dict[str, str]] = []
        if os.name == "nt":
            for letter in string.ascii_uppercase:
                candidate = Path(f"{letter}:\\")
                if candidate.exists():
                    roots.append({"label": f"{letter}:\\", "path": str(candidate)})
        else:
            roots.append({"label": "/", "path": "/"})

        home = Path.home()
        if home.exists():
            roots.insert(0, {"label": "Home", "path": str(home)})
        documents = home / "Documents"
        if documents.exists():
            roots.insert(1, {"label": "Documents", "path": str(documents)})
        return roots

    def _is_project_hint(self, path: Path) -> bool:
        if (path / "package.json").exists() or (path / "index.html").exists():
            return True
        try:
            for item in path.iterdir():
                if item.name.startswith(".") or item.name in _BLOCKED_SEGMENTS:
                    continue
                return True
        except Exception:
            return False
        return False

    def _read_package_json(self) -> dict[str, Any]:
        package_path = self.project_path / "package.json"
        if not package_path.exists():
            return {}
        try:
            return json.loads(package_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _detect_package_manager(self) -> str:
        if (self.project_path / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (self.project_path / "yarn.lock").exists():
            return "yarn"
        if (self.project_path / "bun.lockb").exists() or (self.project_path / "bun.lock").exists():
            return "bun"
        return "npm"

    def _resolve_package_manager_executable(self, manager: str) -> str:
        candidates = {
            "npm": ["npm.cmd", "npm"],
            "pnpm": ["pnpm.cmd", "pnpm"],
            "yarn": ["yarn.cmd", "yarn"],
            "bun": ["bun.exe", "bun"],
        }.get(manager, [manager])

        for candidate in candidates:
            found = shutil.which(candidate)
            if found:
                return found
        return candidates[0]

    def _build_script_command(self, manager: str, script: str, extra_args: list[str]) -> list[str]:
        exe = self._resolve_package_manager_executable(manager)
        if manager == "npm":
            base = [exe, "run", script]
            if extra_args:
                base.extend(["--", *extra_args])
            return base
        if manager == "pnpm":
            return [exe, script, *extra_args]
        if manager == "yarn":
            return [exe, script, *extra_args]
        if manager == "bun":
            return [exe, "run", script, *extra_args]
        return [exe, script, *extra_args]

    def _detect_preview_strategy(self) -> dict[str, Any]:
        static_available = (self.project_path / "index.html").exists()
        package = self._read_package_json()
        scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
        deps = {
            **package.get("dependencies", {}),
            **package.get("devDependencies", {}),
        } if isinstance(package, dict) else {}
        manager = self._detect_package_manager()

        def make_strategy(
            framework: str,
            script: str,
            port: int,
            extra_args: list[str] | None = None,
            env: dict[str, str] | None = None,
        ) -> dict[str, Any]:
            args = extra_args or []
            env_vars = env or {}
            argv = self._build_script_command(manager, script, args)
            return {
                "mode": "dev_server",
                "reason": f"Projeto {framework} detectado via package.json.",
                "can_start": True,
                "framework": framework,
                "package_manager": manager,
                "argv": argv,
                "env": env_vars,
                "default_port": port,
                "display_command": " ".join(argv),
            }

        if scripts:
            script_dev = str(scripts.get("dev", ""))
            script_start = str(scripts.get("start", ""))
            dep_names = {str(name).lower() for name in deps.keys()}
            script_blob = f"{script_dev}\n{script_start}".lower()

            if "next" in dep_names or "next dev" in script_blob:
                return make_strategy("next", "dev", 3000, ["--hostname", "0.0.0.0", "--port", "3000"])
            if "vite" in dep_names or "vite" in script_blob:
                return make_strategy("vite", "dev", 5173, ["--host", "0.0.0.0", "--port", "5173"])
            if "react-scripts" in dep_names or "react-scripts" in script_blob:
                return make_strategy("react-scripts", "start", 3000, env={"HOST": "0.0.0.0", "PORT": "3000", "BROWSER": "none"})
            if "@sveltejs/kit" in dep_names or "svelte-kit" in script_blob:
                return make_strategy("sveltekit", "dev", 5173, ["--host", "0.0.0.0", "--port", "5173"])
            if "astro" in dep_names or "astro dev" in script_blob:
                return make_strategy("astro", "dev", 4321, ["--host", "0.0.0.0", "--port", "4321"])
            if "nuxt" in dep_names or "nuxt dev" in script_blob:
                return make_strategy("nuxt", "dev", 3000, ["--host", "0.0.0.0", "--port", "3000"])
            if "dev" in scripts:
                return make_strategy("npm-app", "dev", 3000)
            if "start" in scripts:
                return make_strategy("npm-app", "start", 3000)

        if static_available:
            return {
                "mode": "static",
                "reason": "Arquivo index.html encontrado na raiz do projeto.",
                "can_start": False,
            }

        return {
            "mode": "none",
            "reason": "Nenhum index.html nem package.json com script detectado.",
            "can_start": False,
        }

    def _append_preview_log(self, message: str) -> None:
        self._preview_logs.append(message.rstrip())
        if len(self._preview_logs) > 200:
            self._preview_logs = self._preview_logs[-200:]

    def _consume_preview_pipe(self, pipe_name: str, pipe) -> None:
        for line in iter(pipe.readline, ""):
            text = line.rstrip()
            if not text:
                continue
            with self._lock:
                self._append_preview_log(f"[{pipe_name}] {text}")
                if self._preview_status == "starting":
                    self._preview_status = "running"
                lowered = text.lower()
                if "error" in lowered or "failed" in lowered:
                    self._preview_error = text
        try:
            pipe.close()
        except Exception:
            pass

    def _apply_commands(
        self,
        commands,
        *,
        execute_run_commands: bool = False,
    ) -> list[dict[str, Any]]:
        backup_dir = self.context_manager.devseek_path / "backups"
        results: list[dict[str, Any]] = []
        wrote_files = False

        for cmd in commands:
            if cmd.action == "run":
                if execute_run_commands:
                    terminal = self.run_terminal_command(cmd.path)
                    results.append({
                        "action": "run",
                        "path": cmd.path,
                        "success": terminal["returncode"] == 0 and not terminal["timed_out"],
                        "message": (
                            f"Executado: {cmd.path}"
                            if not terminal["timed_out"]
                            else f"Tempo esgotado: {cmd.path}"
                        ),
                        "stdout": terminal["stdout"],
                        "stderr": terminal["stderr"],
                        "returncode": terminal["returncode"],
                        "timed_out": terminal["timed_out"],
                    })
                else:
                    results.append({
                        "action": "run",
                        "path": cmd.path,
                        "success": None,
                        "message": f"Pendente: execute manualmente `{cmd.path}`.",
                    })
                continue

            result = apply_command(cmd, str(self.project_path), backup_dir)
            wrote_files = wrote_files or bool(result.success)
            results.append({
                "action": cmd.action,
                "path": cmd.path,
                "dest": getattr(cmd, "dest", ""),
                "success": result.success,
                "message": result.message,
                "diff": result.diff,
            })

        if wrote_files:
            self.context_manager.update_structure()

        return results

    def _record_apply_summary(self, session_id: str, results: list[dict[str, Any]]) -> None:
        if not results:
            return
        total = len(results)
        ok = sum(1 for item in results if item.get("success") is True)
        pending = sum(1 for item in results if item.get("success") is None)
        failed = sum(1 for item in results if item.get("success") is False)
        message = f"Aplicacao concluida: {ok} ok, {pending} pendente(s), {failed} com erro."
        self.history_manager.add_message("Sistema", message, _SYSTEM_COLOR, session_id=session_id)

    def _serialize_message(self, message: dict[str, Any]) -> dict[str, Any]:
        sender = message.get("sender", "")
        raw_text = message.get("text", "")
        if sender == "DeepSeek":
            display_text = extract_chat_text(raw_text) or raw_text
            role = "assistant"
        elif sender == "Sistema":
            display_text = raw_text
            role = "system"
        else:
            display_text = raw_text
            role = "user"
        return {
            "sender": sender,
            "role": role,
            "text": display_text,
            "timestamp": message.get("timestamp", ""),
        }

    def _serialize_command(self, cmd) -> dict[str, Any]:
        summary = cmd.action.replace("_", " ")
        if cmd.action == "move" and cmd.dest:
            summary = f"move {cmd.path} -> {cmd.dest}"
        elif cmd.action == "run":
            summary = f"run {cmd.path}"
        else:
            summary = f"{cmd.action} {cmd.path}"
        return {
            "action": cmd.action,
            "path": cmd.path,
            "dest": getattr(cmd, "dest", ""),
            "summary": summary,
        }

    def _resolve_project_path(self, path: str) -> tuple[Path, str]:
        rel_path = _clean_relative_path(path)
        if not rel_path:
            raise ValueError("Caminho vazio.")

        rel = Path(rel_path)
        if any(part in _BLOCKED_SEGMENTS for part in rel.parts):
            raise PermissionError(rel_path)

        target = (self.project_path / rel).resolve()
        try:
            target.relative_to(self.project_path)
        except ValueError as exc:
            raise PermissionError(rel_path) from exc

        return target, rel.as_posix()

    def _build_tree(self, base: Path) -> list[dict[str, Any]]:
        items = []
        for item in sorted(base.iterdir(), key=lambda entry: (entry.is_file(), entry.name.lower())):
            if item.name in _BLOCKED_SEGMENTS:
                continue
            rel_path = item.relative_to(self.project_path).as_posix()
            node = {
                "name": item.name,
                "path": rel_path,
                "type": "dir" if item.is_dir() else "file",
            }
            if item.is_dir():
                node["children"] = self._build_tree(item)
            items.append(node)
        return items

    def _backup_file(self, target: Path, backup_dir: Path) -> None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{stamp}_{target.name}"
        try:
            shutil.copy2(str(target), str(backup_dir / backup_name))
        except Exception:
            pass
