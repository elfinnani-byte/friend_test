"""서버 메모리에 저장하는 저장소(STORAGE=memory).

노트북에서 바로 실행해 볼 수 있지만, Cloud Run에서는 문제가 생긴다.
- 새 리비전을 배포하거나 인스턴스가 꺼지면 저장한 것이 모두 사라진다.
- 인스턴스가 두 개 이상이면 요청마다 다른 인스턴스로 갈 수 있어 기억이 갈라진다.
그래서 여러 사람이 쓰기 전에는 STORAGE=supabase 로 바꾼다. 두 저장소는 같은 함수 이름을 쓴다.
"""
import itertools
import threading
from datetime import datetime, timezone

from app.store.base import DEFAULT_SETTINGS


def utcnow():
    return datetime.now(timezone.utc)


class MemoryStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        self.users = {}

    def _user(self, uid):
        return self.users.setdefault(uid, {
            "character": None, "settings": dict(DEFAULT_SETTINGS), "messages": [],
            "summary": None, "memories": [], "reminders": []})

    # 캐릭터와 사용자 설정
    def get_character(self, uid):
        return self._user(uid)["character"]

    def save_character(self, uid, character):
        self._user(uid)["character"] = dict(character)

    def get_settings(self, uid):
        return dict(self._user(uid)["settings"])

    def save_settings(self, uid, settings):
        self._user(uid)["settings"].update(settings)

    # 대화
    def add_message(self, uid, role, content, meta=None):
        with self.lock:
            m = {"id": next(self.ids), "role": role, "content": content, "meta": meta or {}, "created_at": utcnow()}
            self._user(uid)["messages"].append(m)
        return m

    def recent_messages(self, uid, limit):
        return list(self._user(uid)["messages"][-limit:])

    def messages_after(self, uid, after_id):
        return [m for m in self._user(uid)["messages"] if m["id"] > (after_id or 0)]

    def get_summary(self, uid):
        return self._user(uid)["summary"]

    def save_summary(self, uid, text, covers_until):
        self._user(uid)["summary"] = {"text": text, "covers_until": covers_until}

    # 기억 메모
    def list_memories(self, uid):
        return [dict(m) for m in self._user(uid)["memories"]]

    def add_memory(self, uid, kind, text):
        with self.lock:
            m = {"id": next(self.ids), "kind": kind, "text": text, "created_at": utcnow(), "updated_at": utcnow()}
            self._user(uid)["memories"].append(m)
        return m

    def update_memory(self, uid, memory_id, text):
        for m in self._user(uid)["memories"]:
            if m["id"] == memory_id:
                m.update(text=text, updated_at=utcnow())

    def delete_memory(self, uid, memory_id):
        u = self._user(uid)
        u["memories"] = [m for m in u["memories"] if m["id"] != memory_id]

    def clear_memories(self, uid):
        self._user(uid)["memories"] = []

    # 약속
    def add_reminder(self, uid, text, due_at):
        with self.lock:
            r = {"id": next(self.ids), "text": text, "due_at": due_at.astimezone(timezone.utc),
                 "status": "pending", "delivered_at": None, "created_at": utcnow()}
            self._user(uid)["reminders"].append(r)
        return dict(r)

    def list_reminders(self, uid, status=None):
        items = [dict(r) for r in self._user(uid)["reminders"] if status in (None, r["status"])]
        return sorted(items, key=lambda r: r["due_at"])

    def cancel_reminder(self, uid, reminder_id):
        for r in self._user(uid)["reminders"]:
            if r["id"] == reminder_id and r["status"] == "pending":
                r["status"] = "cancelled"

    def claim_due_reminders(self, uid, now):
        """때가 된 약속을 '전달됨'으로 바꾸고, 이번에 바꾼 것만 돌려준다(같은 약속을 두 번 전하지 않게)."""
        claimed = []
        with self.lock:
            for r in self._user(uid)["reminders"]:
                if r["status"] == "pending" and r["due_at"] <= now:
                    r.update(status="delivered", delivered_at=utcnow())
                    claimed.append(dict(r))
        return claimed

    def delete_all(self, uid):
        self.users.pop(uid, None)
