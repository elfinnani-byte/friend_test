"""uvicorn 접속 로그에서 결제 주소의 paymentKey 를 가린다(app/logs.py MaskAccessLog)."""
import logging

import pytest

from app import main  # noqa: F401  앱을 불러오면 필터가 걸린다
from app.logs import MaskAccessLog, mask_access_log, mask_query

KEY = "tgen_20261104abcdefghijklmnop"


@pytest.mark.parametrize("path, masked", [
    (f"/buy/success?paymentKey={KEY}&orderId=abc123&amount=1000",
     "/buy/success?paymentKey=tgen***&orderId=abc123&amount=1000"),
    (f"/buy/success?orderId=abc123&paymentKey={KEY}&amount=1000",
     "/buy/success?orderId=abc123&paymentKey=tgen***&amount=1000"),
    (f"/buy/success?orderId=abc123&paymentKey={KEY}", "/buy/success?orderId=abc123&paymentKey=tgen***"),
    ("/buy/fail?code=PAY_PROCESS_CANCELED&orderId=abc123", "/buy/fail?code=PAY_PROCESS_CANCELED&orderId=abc123"),
    ("/api/chat", "/api/chat"),
    (f"/x?mypaymentKey={KEY}", f"/x?mypaymentKey={KEY}"),  # 이름이 정확히 paymentKey 인 것만
])
def test_mask_query(path, masked):
    assert mask_query(path) == masked


def test_access_log_line_hides_payment_key(caplog):
    # uvicorn 이 남기는 것과 같은 모양의 기록을 uvicorn.access 로거로 남겨 본다
    logger = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, MaskAccessLog) for f in logger.filters) == 1
    mask_access_log()  # 다시 불러도 하나만 붙는다
    assert sum(isinstance(f, MaskAccessLog) for f in logger.filters) == 1
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET",
                    f"/buy/success?paymentKey={KEY}&orderId=abc123&amount=1000", "1.1", 200)
    line = caplog.records[-1].getMessage()
    assert line == '127.0.0.1:5000 - "GET /buy/success?paymentKey=tgen***&orderId=abc123&amount=1000 HTTP/1.1" 200'
    assert KEY not in caplog.text
