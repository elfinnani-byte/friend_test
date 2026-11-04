"""크레딧 구매(토스페이먼츠 결제위젯 v2). 토스페이먼츠를 부르지 않고 가짜 클라이언트를 끼워 확인한다.
테스트마다 새 메모리 저장소를 쓴다."""
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main, payments
from app.config import FREE_SIGNUP_CREDITS, PRODUCTS
from app.store.memory_store import MemoryStore

UID = "local-user"
client = TestClient(main.app)


class FakeToss:
    """결제 승인과 조회를 흉내 낸다. confirm_error 를 주면 승인 API 가 그 오류를 낸다."""

    def __init__(self, confirm_error=None, query=None, query_error=None):
        self.confirm_error, self.query, self.query_error = confirm_error, query, query_error
        self.confirms, self.queries = [], []

    def confirm(self, payment_key, order_id, amount):
        self.confirms.append((payment_key, order_id, amount))
        if self.confirm_error:
            raise self.confirm_error
        return {"paymentKey": payment_key, "orderId": order_id, "status": "DONE", "totalAmount": amount}

    def get_by_order(self, order_id):
        self.queries.append(order_id)
        if self.query_error:
            raise self.query_error
        return self.query


@pytest.fixture
def shop(monkeypatch):
    store, toss = MemoryStore(), FakeToss()
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "toss", toss)
    monkeypatch.setattr(main.settings, "payments_enabled", True)
    monkeypatch.setattr(main.settings, "toss_client_key", "fake-toss-client-key-for-tests")
    return store, toss


def order(product_id="credits_100"):
    r = client.post("/api/orders", json={"product_id": product_id})
    assert r.status_code == 200
    return r.json()


def confirm(order_id, amount, payment_key="fake_payment_key"):
    return client.post("/api/orders/confirm", json={"payment_key": payment_key, "order_id": order_id, "amount": amount})


def balance():
    return client.get("/api/credits").json()["balance"]


# ── 상품표 ──────────────────────────────────────────────────────────

def test_products_come_from_one_table():
    body = client.get("/api/products").json()
    assert body["cost_per_message"] == 1
    assert body["products"] == [
        {"id": "credits_100", "name": "크레딧 100개", "amount": 1000, "credits": 100, "unit_price": 10.0},
        {"id": "credits_550", "name": "크레딧 550개", "amount": 5000, "credits": 550, "unit_price": 9.09},
    ]
    assert [p["id"] for p in body["products"]] == list(PRODUCTS)


# ── 주문 만들기 ─────────────────────────────────────────────────────

def test_order_amount_comes_from_server_table(shop):
    store, _ = shop
    o = order("credits_550")
    assert (o["amount"], o["order_name"]) == (5000, "크레딧 550개")
    saved = store.get_order(UID, o["order_id"])
    assert (saved["amount"], saved["credits"], saved["status"]) == (5000, 550, "pending")
    assert 6 <= len(o["order_id"]) <= 64 and o["order_id"].isalnum()  # 토스페이먼츠 orderId 규칙


def test_unknown_product_is_rejected(shop):
    r = client.post("/api/orders", json={"product_id": "credits_999"})
    assert r.status_code == 404 and r.json()["error"] == "unknown_product"


def test_orders_are_off_without_keys(monkeypatch):
    monkeypatch.setattr(main.settings, "payments_enabled", False)
    r = client.post("/api/orders", json={"product_id": "credits_100"})
    assert r.status_code == 503 and r.json()["error"] == "payments_disabled"
    assert confirm("x" * 32, 1000).status_code == 503


# ── 승인과 지급 ─────────────────────────────────────────────────────

def test_confirm_grants_credits_once(shop):
    store, toss = shop
    start = balance()
    o = order()
    r = confirm(o["order_id"], 1000)
    assert r.status_code == 200 and r.json() == {"ok": True, "credits": 100, "balance": start + 100}
    assert toss.confirms == [("fake_payment_key", o["order_id"], 1000)]
    saved = store.get_order(UID, o["order_id"])
    assert (saved["status"], saved["payment_key"]) == ("paid", "fake_payment_key")
    latest = client.get("/api/credits").json()["recent"][0]
    assert (latest["delta"], latest["reason"], latest["ref"]) == (100, "purchase", f"order:{o['order_id']}")

    # 성공 페이지를 새로 고쳐도 다시 승인하거나 다시 지급하지 않는다
    again = confirm(o["order_id"], 1000)
    assert again.status_code == 200 and again.json()["balance"] == start + 100
    assert len(toss.confirms) == 1


def test_amount_mismatch_is_not_confirmed(shop):
    store, toss = shop
    o = order()
    r = confirm(o["order_id"], 100)  # 브라우저에서 금액을 바꾼 경우
    assert r.status_code == 400 and r.json()["error"] == "amount_mismatch"
    assert toss.confirms == []  # 승인 API 를 부르지 않는다
    assert store.get_order(UID, o["order_id"])["status"] == "failed"
    assert balance() == FREE_SIGNUP_CREDITS


def test_other_users_order_is_not_found(shop):
    store, toss = shop
    store.create_order("someone-else", "otherorder123", "credits_100", 1000, 100)
    assert confirm("otherorder123", 1000).status_code == 404
    assert toss.confirms == []


def test_failed_confirm_checks_payment_before_failing(shop):
    store, toss = shop
    toss.confirm_error = payments.TossError(400, "REJECT_CARD_COMPANY", "카드사에서 거절했어요.")
    toss.query = {"orderId": "x", "status": "ABORTED", "totalAmount": 1000}
    o = order()
    r = confirm(o["order_id"], 1000)
    assert r.status_code == 400
    assert r.json() == {"error": "payment_failed", "message": "카드사에서 거절했어요.", "code": "REJECT_CARD_COMPANY"}
    assert toss.queries == [o["order_id"]]
    assert store.get_order(UID, o["order_id"])["status"] == "failed"
    assert balance() == FREE_SIGNUP_CREDITS


def test_confirm_error_but_payment_done_still_grants(shop):
    # 승인 응답을 못 받았지만 실제로는 승인된 경우: 조회로 확인하고 지급한다
    store, toss = shop
    o = order()
    toss.confirm_error = payments.TossError(None, "ReadTimeout", "토스페이먼츠에 연결하지 못했어요.")
    toss.query = {"paymentKey": "fake_payment_key", "orderId": o["order_id"], "status": "DONE", "totalAmount": 1000}
    r = confirm(o["order_id"], 1000)
    assert r.status_code == 200 and r.json()["credits"] == 100
    assert store.get_order(UID, o["order_id"])["status"] == "paid"


def test_payment_not_found_on_query_is_failure(shop):
    store, toss = shop
    toss.confirm_error = payments.TossError(404, "NOT_FOUND_PAYMENT_SESSION", "결제 시간이 만료되었습니다.")
    toss.query_error = payments.TossError(404, "NOT_FOUND_PAYMENT", "존재하지 않는 결제 정보 입니다.")
    o = order()
    r = confirm(o["order_id"], 1000)
    assert r.status_code == 400 and r.json()["code"] == "NOT_FOUND_PAYMENT_SESSION"
    assert store.get_order(UID, o["order_id"])["status"] == "failed"


def test_unknown_result_keeps_order_pending(shop):
    store, toss = shop
    toss.confirm_error = payments.TossError(None, "ReadTimeout", "")
    toss.query_error = payments.TossError(None, "ConnectError", "")
    o = order()
    r = confirm(o["order_id"], 1000)
    assert r.status_code == 502 and r.json()["error"] == "confirm_unknown"
    assert store.get_order(UID, o["order_id"])["status"] == "pending"  # 새로 고치면 다시 확인할 수 있다
    assert balance() == FREE_SIGNUP_CREDITS


# ── 토스페이먼츠 클라이언트 ─────────────────────────────────────────

def test_toss_client_uses_basic_auth_with_colon_and_idempotency_key():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "DONE"})

    toss = payments.TossClient("fake-toss-secret-for-tests")
    toss.http = httpx.Client(base_url=payments.API, headers=toss.http.headers, transport=httpx.MockTransport(handler))
    toss.confirm("pk", "order123", 1000)
    req = seen[0]
    assert req.url == "https://api.tosspayments.com/v1/payments/confirm" and req.method == "POST"
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b"fake-toss-secret-for-tests:").decode()
    assert req.headers["idempotency-key"] == "confirm-order123"


SECRET = "fake-toss-secret-for-tests"
PAYMENT_KEY = "fake_payment_key_full_value_123"


def scripted_toss(*responses):
    """정해 둔 응답을 차례로 돌려주는 토스 클라이언트. 기다리지 않는다. (보낸 요청 목록, 클라이언트)"""
    seen, queue = [], list(responses)

    def handler(request):
        seen.append(request)
        return queue.pop(0)

    toss = payments.TossClient(SECRET)
    toss.http = httpx.Client(base_url=payments.API, headers=toss.http.headers, transport=httpx.MockTransport(handler))
    toss.sleep = lambda seconds: None
    return seen, toss


PROCESSING = httpx.Response(409, json={"code": "IDEMPOTENT_REQUEST_PROCESSING", "message": "이전 요청이 처리 중입니다."})
DONE = httpx.Response(200, json={"status": "DONE", "orderId": "order123", "totalAmount": 1000})


def test_same_order_always_sends_same_idempotency_key():
    seen, toss = scripted_toss(DONE, DONE, DONE)
    for _ in range(3):
        toss.confirm(PAYMENT_KEY, "order123", 1000)
    assert {r.headers["idempotency-key"] for r in seen} == {"confirm-order123"}
    assert payments.idempotency_key("order123") == "confirm-order123"
    assert payments.idempotency_key("order456") != payments.idempotency_key("order123")


def test_processing_409_is_retried_with_same_key():
    seen, toss = scripted_toss(PROCESSING, PROCESSING, DONE)
    waits = []
    toss.sleep = waits.append
    assert toss.confirm(PAYMENT_KEY, "order123", 1000)["status"] == "DONE"
    assert len(seen) == 3 and {r.headers["idempotency-key"] for r in seen} == {"confirm-order123"}
    assert waits == list(payments.CONFIRM_RETRY_WAITS[:2])


def test_processing_409_gives_up_after_retries():
    tries = len(payments.CONFIRM_RETRY_WAITS) + 1
    seen, toss = scripted_toss(*[PROCESSING] * tries)
    with pytest.raises(payments.TossError) as caught:
        toss.confirm(PAYMENT_KEY, "order123", 1000)
    assert (caught.value.status, caught.value.code) == (409, "IDEMPOTENT_REQUEST_PROCESSING")
    assert len(seen) == tries


def test_other_errors_are_not_retried():
    seen, toss = scripted_toss(httpx.Response(400, json={"code": "REJECT_CARD_COMPANY", "message": "거절"}))
    with pytest.raises(payments.TossError):
        toss.confirm(PAYMENT_KEY, "order123", 1000)
    assert len(seen) == 1


def test_each_confirm_call_logs_order_and_key_but_not_secrets(capsys):
    _, toss = scripted_toss(PROCESSING, DONE)
    toss.confirm(PAYMENT_KEY, "order123", 1000)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    calls = [line for line in lines if line["message"] == "결제 승인 API 호출"]
    assert [(c["order_id"], c["idempotency_key"], c["attempt"], c["status"]) for c in calls] == \
        [("order123", "confirm-order123", 1, 409), ("order123", "confirm-order123", 2, 200)]
    out = json.dumps(lines, ensure_ascii=False)
    token = base64.b64encode(f"{SECRET}:".encode()).decode()
    assert SECRET not in out and token not in out and PAYMENT_KEY not in out


def test_toss_client_error_has_code_and_message():
    toss = payments.TossClient("fake-toss-secret-for-tests")
    toss.http = httpx.Client(base_url=payments.API, transport=httpx.MockTransport(
        lambda r: httpx.Response(404, json={"code": "NOT_FOUND_PAYMENT_SESSION", "message": "결제 시간이 만료되었습니다."})))
    with pytest.raises(payments.TossError) as caught:
        toss.confirm("pk", "order123", 1000)
    assert (caught.value.status, caught.value.code) == (404, "NOT_FOUND_PAYMENT_SESSION")


# ── 화면 설정과 키 ──────────────────────────────────────────────────

def test_config_has_client_key_but_never_secret(shop, monkeypatch):
    monkeypatch.setattr(main.settings, "toss_secret_key", "fake-toss-secret-for-tests")
    body = client.get("/api/config").json()
    assert body["payments_enabled"] is True and body["toss_client_key"] == "fake-toss-client-key-for-tests"
    assert "fake-toss-secret-for-tests" not in str(body)


@pytest.mark.parametrize("client_key, secret_key, enabled", [
    ("test_gck_a", "test_gsk_b", True),
    ("", "test_gsk_b", False),
    ("test_gck_a", "", False),
    ("live_gck_a", "live_gsk_b", False),   # 이번 단계는 테스트 키만
    ("test_gck_a", "live_gsk_b", False),
])
def test_payments_on_only_with_both_test_keys(monkeypatch, client_key, secret_key, enabled):
    from app.config import load_settings
    monkeypatch.setenv("TOSS_CLIENT_KEY", client_key)
    monkeypatch.setenv("TOSS_SECRET_KEY", secret_key)
    s = load_settings()
    assert s.payments_enabled is enabled
    assert (s.toss_secret_key == secret_key) if enabled else s.toss_secret_key == ""


def test_low_flag_comes_from_server(shop):
    store, _ = shop
    assert client.get("/api/credits").json()["low"] is False
    store.add_credit(UID, -(balance() - 10), "message", "test:leave-ten")
    assert client.get("/api/credits").json()["low"] is True


@pytest.mark.parametrize("path", ["/buy/success", "/buy/fail"])
def test_result_pages_are_served(path):
    r = client.get(path)
    assert r.status_code == 200 and "/api/orders/confirm" in r.text
