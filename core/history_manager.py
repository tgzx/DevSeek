import json
from datetime import datetime
from pathlib import Path

HISTORY_FILE = "history.json"


class HistoryManager:
    """
    Persiste o histórico de conversas em .devseek/history.json.
    Cada sessão é identificada por um timestamp ISO e contém uma
    lista de mensagens {sender, text, color, timestamp}.
    """

    def __init__(self, devseek_path: Path):
        self._path = devseek_path / HISTORY_FILE
        self._data: dict = {"sessions": []}
        self._current_id: str | None = None
        self._load()

    # ── Session management ────────────────────────────────────────────────────

    def new_session(self) -> str:
        sid = datetime.now().isoformat(timespec="seconds")
        self._data["sessions"].append({"id": sid, "messages": []})
        self._current_id = sid
        self._save()
        return sid

    def resume_last_session(self) -> str | None:
        """Retoma a última sessão existente ou cria uma nova."""
        sessions = self._data["sessions"]
        if sessions:
            self._current_id = sessions[-1]["id"]
        else:
            self.new_session()
        return self._current_id

    @property
    def current_session_id(self) -> str | None:
        return self._current_id

    # ── Message persistence ───────────────────────────────────────────────────

    def add_message(
        self,
        sender: str,
        text: str,
        color: str,
        session_id: str | None = None,
        metadata: dict | None = None,
    ):
        sid = session_id or self._current_id
        if not sid:
            sid = self.new_session()
        session = self._get_session(sid)
        if session is None:
            return
        payload = {
            "sender": sender,
            "text": text,
            "color": color,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
        if metadata:
            payload.update(metadata)
        session["messages"].append(payload)
        if session_id is None:
            self._current_id = sid
        self._save()

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_sessions(self) -> list[dict]:
        """Retorna sessões mais recentes primeiro, com preview."""
        result = []
        for s in reversed(self._data["sessions"]):
            msgs = s.get("messages", [])
            first_q = next(
                (m["text"][:80] for m in msgs if m["sender"] == "Você"), ""
            )
            result.append({
                "id": s["id"],
                "date": s["id"],
                "message_count": len(msgs),
                "preview": first_q,
            })
        return result

    def get_messages(self, session_id: str) -> list[dict]:
        s = self._get_session(session_id)
        return s["messages"] if s else []

    def get_current_messages(self) -> list[dict]:
        if not self._current_id:
            return []
        return self.get_messages(self._current_id)

    def delete_session(self, session_id: str):
        self._data["sessions"] = [
            s for s in self._data["sessions"] if s["id"] != session_id
        ]
        if self._current_id == session_id:
            self._current_id = None
        self._save()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _get_session(self, sid: str) -> dict | None:
        for s in self._data["sessions"]:
            if s["id"] == sid:
                return s
        return None

    def _load(self):
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
                if "sessions" not in self._data:
                    self._data = {"sessions": []}
            except Exception:
                self._data = {"sessions": []}

    def _save(self):
        try:
            self._path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass
