import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.mobile_bridge import BridgeService
from tests.fixtures import SINGLE_FILE_CREATE


def _wait_for_job(service: BridgeService, job_id: str, timeout_s: float = 5.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        job = service.get_job(job_id)
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} nao concluiu no tempo esperado.")


def _fake_send(response: str, sink: list[str] | None = None):
    def _send(prompt: str, **kwargs):
        if sink is not None:
            sink.append(prompt)
        callback = kwargs.get("status_callback")
        if callback:
            callback("Enviando prompt de teste...")
            callback("Resposta sintetica pronta.")
        return response
    return _send


class TestBridgeChatFlow:
    def test_auto_apply_creates_file(self, tmp_project):
        prompts: list[str] = []
        service = BridgeService(str(tmp_project), send_prompt_fn=_fake_send(SINGLE_FILE_CREATE, prompts))

        job = service.submit_chat(
            "Crie um arquivo hello.py",
            include_search=False,
            auto_apply=True,
        )
        result = _wait_for_job(service, job["job_id"])

        assert result["status"] == "completed"
        assert result["apply_status"] == "applied"
        assert (tmp_project / "hello.py").exists()
        assert "[DEVSEEK_FIM]" in prompts[0]

    def test_manual_apply_waits_for_user(self, tmp_project):
        service = BridgeService(str(tmp_project), send_prompt_fn=_fake_send(SINGLE_FILE_CREATE))

        job = service.submit_chat(
            "Crie um arquivo hello.py",
            include_search=False,
            auto_apply=False,
        )
        result = _wait_for_job(service, job["job_id"])

        assert result["status"] == "completed"
        assert result["apply_status"] == "pending"
        assert not (tmp_project / "hello.py").exists()

        applied = service.apply_job(job["job_id"])
        assert applied["apply_status"] == "applied"
        assert (tmp_project / "hello.py").exists()

    def test_state_returns_clean_assistant_message(self, tmp_project):
        service = BridgeService(str(tmp_project), send_prompt_fn=_fake_send(SINGLE_FILE_CREATE))

        job = service.submit_chat("Teste de exibicao", include_search=False)
        _wait_for_job(service, job["job_id"])
        state = service.get_state()

        assistant = next(item for item in state["messages"] if item["role"] == "assistant")
        assert "DEVSEEK_CREATE" not in assistant["text"]
        assert "DEVSEEK_FIM" not in assistant["text"]


class TestBridgeFilesAndPreview:
    def test_editor_roundtrip(self, tmp_project):
        service = BridgeService(str(tmp_project), send_prompt_fn=_fake_send(SINGLE_FILE_CREATE))

        service.write_file("pages/index.html", "<h1>Bridge</h1>")
        data = service.read_file("pages/index.html")

        assert data["path"] == "pages/index.html"
        assert "<h1>Bridge</h1>" in data["content"]

    def test_tree_lists_saved_file(self, tmp_project):
        service = BridgeService(str(tmp_project), send_prompt_fn=_fake_send(SINGLE_FILE_CREATE))
        service.write_file("src/app.js", "console.log('ok');")

        items = service.list_files()
        src_dir = next(item for item in items if item["name"] == "src")
        assert src_dir["type"] == "dir"
        assert any(child["path"] == "src/app.js" for child in src_dir["children"])

    def test_preview_defaults_to_root_index(self, tmp_project):
        service = BridgeService(str(tmp_project), send_prompt_fn=_fake_send(SINGLE_FILE_CREATE))
        target = tmp_project / "index.html"
        target.write_text("<html></html>", encoding="utf-8")

        assert service.resolve_preview_path() == target.resolve()

    def test_preview_detects_vite_project(self, tmp_project):
        service = BridgeService(str(tmp_project), send_prompt_fn=_fake_send(SINGLE_FILE_CREATE))
        (tmp_project / "package.json").write_text(
            """
            {
              "scripts": { "dev": "vite" },
              "devDependencies": { "vite": "^5.0.0" }
            }
            """,
            encoding="utf-8",
        )

        preview = service.get_preview_state()
        assert preview["suggested_mode"] == "dev_server"
        assert preview["dev_server"]["can_start"] is True
        assert preview["dev_server"]["recommended_port"] == 5173


class TestBridgeProjectSelection:
    def test_create_project_switches_current_root(self, tmp_path):
        initial = tmp_path / "initial"
        initial.mkdir()
        service = BridgeService(str(initial), send_prompt_fn=_fake_send(SINGLE_FILE_CREATE))

        state = service.create_project(str(tmp_path), "novo-app")

        assert (tmp_path / "novo-app").is_dir()
        assert state["project_name"] == "novo-app"
        assert Path(state["project_path"]) == (tmp_path / "novo-app").resolve()

    def test_open_project_changes_explorer_root(self, tmp_path):
        first = tmp_path / "primeiro"
        second = tmp_path / "segundo"
        first.mkdir()
        second.mkdir()
        (second / "index.html").write_text("<html>ok</html>", encoding="utf-8")

        service = BridgeService(str(first), send_prompt_fn=_fake_send(SINGLE_FILE_CREATE))
        state = service.open_project(str(second))

        assert state["project_name"] == "segundo"
        assert service.resolve_preview_path().name == "index.html"

    def test_browse_projects_lists_subdirectories(self, tmp_path):
        project = tmp_path / "project"
        alpha = tmp_path / "alpha"
        beta = tmp_path / "beta"
        project.mkdir()
        alpha.mkdir()
        beta.mkdir()
        (beta / "package.json").write_text('{"scripts": {"dev": "vite"}}', encoding="utf-8")

        service = BridgeService(str(project), send_prompt_fn=_fake_send(SINGLE_FILE_CREATE))
        browser = service.browse_projects(str(tmp_path))

        names = {item["name"] for item in browser["items"]}
        assert {"project", "alpha", "beta"} <= names
        beta_item = next(item for item in browser["items"] if item["name"] == "beta")
        assert beta_item["has_package_json"] is True
