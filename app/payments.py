"""크레딧 구매. 토스페이먼츠 결제위젯 v2(주문서형 결제)로 결제하고, 승인되면 크레딧을 원장에 더한다.

흐름(근거: 토스페이먼츠 MCP 의 결제위젯 v2 문서)
  1. 화면이 상품을 고르면 서버가 주문을 만든다(create_order). 금액은 서버의 상품표(PRODUCTS)에서 정한다.
  2. 화면이 widgets.requestPayment({orderId, orderName, successUrl, failUrl}) 로 결제를 요청한다.
  3. 인증이 끝나면 successUrl(/buy/success?paymentKey=&orderId=&amount=) 로 돌아온다.
  4. 서버가 amount 를 저장한 주문 금액과 비교하고, 같을 때만 결제 승인 API 를 부른다(confirm_order).
     "Verify in the server that amount from successUrl matches the merchant's intended amount" — LLM Quick Reference §2.2
  5. 승인되면(status DONE) 크레딧을 원장에 더하고(ref order:<주문ID>) 주문을 paid 로 바꾼다.

- 승인 API 는 시크릿 키로 Basic base64(시크릿키 + ":") 인증을 한다(§2.3). 시크릿 키는 이 파일에서만 쓴다.
- 승인 요청에는 주문마다 같은 Idempotency-Key 를 붙여, 성공 페이지를 새로 고쳐도 두 번 승인하지 않는다(§7.1).
- 승인 API 가 실패해도 바로 실패로 보지 않고 주문번호로 결제를 조회해 확인한다(§8 Common Mistakes).
- 크레딧 지급은 ref order:<주문ID> 가 유일하므로 여러 번 불려도 한 번만 들어간다.

문서:
  https://docs.tosspayments.com/guides/v2/payment-widget
  https://docs.tosspayments.com/guides/v2/payment-widget/integration-window (리다이렉트, 결제 승인, 응답 확인)
  https://docs.tosspayments.com/sdk/v2/js/payment-widget
  https://docs.tosspayments.com/guides/v2/get-started/llms-quick-reference
  https://docs.tosspayments.com/reference/error-codes
  https://docs.tosspayments.com/reference/using-api/authorization (멱등키 헤더, 409 IDEMPOTENT_REQUEST_PROCESSING)
"""
import base64
import time
import uuid

import httpx

from app.config import PRODUCTS
from app.logs import log
from app.store.base import DuplicateReference

API = "https://api.tosspayments.com"
CONFIRM_RETRY_WAITS = (1, 2, 4)  # 409(같은 요청 처리 중)일 때 다시 묻기 전 기다리는 초. 최대 3번 더 묻는다


def idempotency_key(order_id):
    """결제 승인의 멱등키. 주문 번호(서버가 만든 uuid4)로 만들어, 같은 주문이면 늘 같은 값이다(15일간 유효)."""
    return f"confirm-{order_id}"


class TossError(Exception):
    """토스페이먼츠 API 오류. status 가 None 이면 응답을 받지 못했다(연결 실패, 시간 초과)."""

    def __init__(self, status, code, message):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


class TossClient:
    def __init__(self, secret_key):
        token = base64.b64encode(f"{secret_key}:".encode()).decode()  # 콜론을 빠뜨리지 않는다
        self.http = httpx.Client(base_url=API, timeout=30,
                                 headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"})

    def _call(self, method, path, **kwargs):
        try:
            r = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise TossError(None, type(error).__name__, "토스페이먼츠에 연결하지 못했어요.") from None
        if r.status_code != 200:
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            raise TossError(r.status_code, body.get("code", "UNKNOWN"), body.get("message", ""))
        return r.json()

    def confirm(self, payment_key, order_id, amount):
        """결제 승인 API. POST /v1/payments/confirm

        같은 주문이면 몇 번을 보내도 같은 멱등키가 간다. 409 IDEMPOTENT_REQUEST_PROCESSING(같은 요청이 처리 중)이면
        잠시 뒤 같은 키로 다시 보내 응답을 확인한다. 키를 바꿔 다시 보내지 않는다(문서: 멱등키 헤더).
        """
        key = idempotency_key(order_id)
        for attempt, wait in enumerate((*CONFIRM_RETRY_WAITS, None), start=1):
            try:
                result = self._call("POST", "/v1/payments/confirm", headers={"Idempotency-Key": key},
                                    json={"paymentKey": payment_key, "orderId": order_id, "amount": amount})
            except TossError as error:
                # 시크릿 키와 paymentKey 는 남기지 않는다
                log("INFO", "결제 승인 API 호출", order_id=order_id, idempotency_key=key, attempt=attempt,
                    status=error.status, code=error.code)
                if error.status == 409 and error.code == "IDEMPOTENT_REQUEST_PROCESSING" and wait is not None:
                    self.sleep(wait)
                    continue
                raise
            log("INFO", "결제 승인 API 호출", order_id=order_id, idempotency_key=key, attempt=attempt, status=200)
            return result

    sleep = staticmethod(time.sleep)  # 테스트에서 기다리지 않게 바꿔 끼운다

    def get_by_order(self, order_id):
        """주문번호로 결제 조회. GET /v1/payments/orders/{orderId}"""
        return self._call("GET", f"/v1/payments/orders/{order_id}")


class PaymentError(Exception):
    """화면에 돌려줄 결제 오류. status 는 HTTP 상태, error 는 종류, message 는 사용자에게 보일 문장."""

    def __init__(self, status, error, message, code=None):
        super().__init__(error)
        self.status, self.error, self.message, self.code = status, error, message, code

    def body(self):
        return {"error": self.error, "message": self.message, **({"code": self.code} if self.code else {})}


def product_list():
    """GET /api/products 가 내려 줄 상품표. 크레딧 1개 가격은 여기서 계산한다."""
    return [{"id": pid, "name": p["name"], "amount": p["amount"], "credits": p["credits"],
             "unit_price": round(p["amount"] / p["credits"], 2)} for pid, p in PRODUCTS.items()]


def create_order(store, uid, product_id):
    product = PRODUCTS.get(product_id)
    if not product:
        raise PaymentError(404, "unknown_product", "없는 상품이에요.")
    order = store.create_order(uid, uuid.uuid4().hex, product_id, product["amount"], product["credits"])
    log("INFO", "주문 만듦", product_id=product_id)
    return {"order_id": order["id"], "order_name": product["name"], "amount": order["amount"]}


def _approved(payment, order):
    return (payment.get("status") == "DONE" and payment.get("orderId") == order["id"]
            and payment.get("totalAmount") == order["amount"])


def confirm_order(store, uid, toss, order_id, payment_key, amount):
    """successUrl 로 돌아온 결제를 확인하고 승인한다. 성공하면 지급한 크레딧 수를 돌려준다."""
    order = store.get_order(uid, order_id)
    if not order:
        raise PaymentError(404, "unknown_order", "주문을 찾을 수 없어요.")
    if order["status"] == "paid":
        return order["credits"]  # 이미 승인하고 지급했다(성공 페이지를 새로 고침)
    if order["status"] == "failed":
        raise PaymentError(409, "order_failed", "이미 실패한 주문이에요. 처음부터 다시 결제해 주세요.")

    # 결제 금액 검증: successUrl 의 amount 가 서버에 저장한 주문 금액과 다르면 승인하지 않는다
    if amount != order["amount"]:
        store.update_order(uid, order_id, "failed")
        log("WARNING", "결제 금액 불일치로 승인하지 않음", product_id=order["product_id"])
        raise PaymentError(400, "amount_mismatch", "결제 금액이 주문 금액과 달라 결제를 진행하지 않았어요.")

    try:
        payment = toss.confirm(payment_key, order_id, order["amount"])
    except TossError as error:
        log("WARNING", "결제 승인 실패, 결제 조회로 확인", status=error.status, code=error.code)
        try:
            payment = toss.get_by_order(order_id)
        except TossError as query_error:
            if query_error.code == "NOT_FOUND_PAYMENT":  # 에러 코드 문서: 존재하지 않는 결제 정보 → 승인되지 않았다
                payment = {}
            else:
                # 승인됐는지 알 수 없다. 주문을 pending 으로 두어 다시 시도할 수 있게 한다
                log("ERROR", "결제 조회 실패", status=query_error.status, code=query_error.code)
                raise PaymentError(502, "confirm_unknown", "결제 결과를 확인하지 못했어요. 잠시 뒤 이 페이지를 새로 고쳐 주세요.") from None
        if not _approved(payment, order):
            store.update_order(uid, order_id, "failed")
            raise PaymentError(400, "payment_failed", error.message or "결제를 승인하지 못했어요.", code=error.code) from None

    if not _approved(payment, order):
        store.update_order(uid, order_id, "failed")
        log("ERROR", "승인 응답이 주문과 다름", status=payment.get("status"))
        raise PaymentError(400, "payment_failed", "결제를 승인하지 못했어요.")

    # 지급을 먼저 하고 paid 로 바꾼다. 그 사이에 멈춰도 다시 부르면 지급은 ref 로 한 번만 들어가고 paid 가 된다
    try:
        store.add_credit(uid, order["credits"], "purchase", f"order:{order_id}")
    except DuplicateReference:
        pass
    store.update_order(uid, order_id, "paid", payment_key=payment.get("paymentKey", payment_key))
    log("INFO", "크레딧 구매 완료", product_id=order["product_id"], credits=order["credits"])
    return order["credits"]
