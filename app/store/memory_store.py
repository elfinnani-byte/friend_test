"""서버 메모리에 저장하는 저장소(STORAGE=memory).

노트북에서 바로 실행해 볼 수 있지만, Cloud Run에서는 문제가 생긴다.
- 새 리비전을 배포하거나 인스턴스가 꺼지면 저장한 것이 모두 사라진다.
- 인스턴스가 두 개 이상이면 요청마다 다른 인스턴스로 갈 수 있어 기억이 갈라진다.
그래서 여러 사람이 쓰기 전에는 STORAGE=supabase 로 바꾼다. 두 저장소는 같은 함수 이름을 쓴다.
"""
import itertools
import threading
from datetime import datetime, timezone

from app.store.base import DEFAULT_SETTINGS, DuplicateReference


def utcnow():
    return datetime.now(timezone.utc)


class MemoryStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        self.users = {}
        # 크레딧 원장은 사용자 데이터와 따로 둔다. 내 데이터 삭제(delete_all)로 지워지면 무료 크레딧을 다시 받게 된다
        self.ledger = {}      # uid -> 기록 목록
        self.refs = set()     # 모든 사용자의 ref. 데이터베이스의 unique 제약처럼 겹치면 거절한다
        self.orders = {}      # 주문 ID -> 주문(user_id 포함). 원장처럼 내 데이터 삭제로 지우지 않는다

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

    def get_message(self, uid, message_id):
        """이 사용자의 메시지만 찾는다. 남의 메시지 ID면 None."""
        return next((dict(m) for m in self._user(uid)["messages"] if m["id"] == message_id), None)

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

    # 크레딧 원장
    def credit_balance(self, uid):
        return sum(e["delta"] for e in self.ledger.get(uid, []))

    def add_credit(self, uid, delta, reason, ref):
        """기록 한 줄을 더한다. ref 가 이미 있으면 DuplicateReference."""
        with self.lock:
            if ref in self.refs:
                raise DuplicateReference(ref)
            e = {"id": next(self.ids), "delta": int(delta), "reason": reason, "ref": ref, "created_at": utcnow()}
            self.refs.add(ref)
            self.ledger.setdefault(uid, []).append(e)
        return dict(e)

    def recent_credits(self, uid, limit):
        """최근 기록부터 limit 줄."""
        return [dict(e) for e in reversed(self.ledger.get(uid, [])[-limit:])]

    # 크레딧 구매 주문
    def create_order(self, uid, order_id, product_id, amount, credits):
        with self.lock:
            if order_id in self.orders:
                raise ValueError("이미 있는 주문 ID")
            o = {"id": order_id, "user_id": uid, "product_id": product_id, "amount": int(amount),
                 "credits": int(credits), "status": "pending", "payment_key": None, "created_at": utcnow(),
                 "paid_at": None}
            self.orders[order_id] = o
        return dict(o)

    def get_order(self, uid, order_id):
        """이 사용자의 주문만 찾는다. 남의 주문 ID면 None."""
        o = self.orders.get(order_id)
        return dict(o) if o and o["user_id"] == uid else None

    def update_order(self, uid, order_id, status, payment_key=None):
        """pending 인 주문만 바꾼다. 바꿨으면 바뀐 주문, 이미 다른 상태면 None."""
        with self.lock:
            o = self.orders.get(order_id)
            if not o or o["user_id"] != uid or o["status"] != "pending":
                return None
            o.update(status=status, payment_key=payment_key, paid_at=utcnow() if status == "paid" else None)
            return dict(o)

    def delete_all(self, uid):
        self.users.pop(uid, None)
