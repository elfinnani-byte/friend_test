"""로그 한 줄을 JSON으로 남긴다.

Cloud Run은 JSON 로그의 severity 값을 읽어 로그 화면의 심각도(정보, 경고, 오류)로 보여 준다.
키나 문의 원문 같은 값은 로그에 남기지 않는다.
"""
import json
import logging
import re
import sys


def log(severity, message, **fields):
    record = {"severity": severity, "message": message, **fields}
    print(json.dumps(record, ensure_ascii=False), file=sys.stdout, flush=True)


# ── uvicorn 접속 로그에서 paymentKey 가리기 ─────────────────────────
# 토스페이먼츠는 결제 뒤 /buy/success?paymentKey=...&orderId=...&amount=... 로 돌아온다.
# uvicorn 접속 로그는 주소를 쿼리까지 그대로 남기므로, 기록하기 전에 paymentKey 값을 앞 4글자만 남기고 가린다.
SECRET_QUERY = re.compile(r"(?<=[?&])(paymentKey=)([^&#\s]*)")


def mask_query(path):
    return SECRET_QUERY.sub(lambda m: m.group(1) + m.group(2)[:4] + "***", path)


class MaskAccessLog(logging.Filter):
    """uvicorn.access 로그의 인자(클라이언트, 메서드, 주소, HTTP 버전, 상태)에서 주소의 paymentKey 를 가린다."""

    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) >= 3 and isinstance(record.args[2], str):
            record.args = (*record.args[:2], mask_query(record.args[2]), *record.args[3:])
        return True


def mask_access_log():
    """uvicorn 이 어떻게 실행되든(로컬, Dockerfile 의 CMD) 앱을 불러올 때 한 번 건다. 여러 번 불러도 하나만 붙는다."""
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, MaskAccessLog) for f in logger.filters):
        logger.addFilter(MaskAccessLog())
