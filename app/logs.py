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


# ── uvicorn 접속 로그에서 토큰 가리기 ───────────────────────────────
# /api/quiz/auto/status?token=... 처럼 주소에 팀 토큰이 들어오면 접속 로그에 그대로 남으므로, 기록하기 전에 가린다.
# (Cloud Run 플랫폼의 요청 로그는 앱 밖이라 이 필터로 가려지지 않는다. 앱은 Authorization 헤더로 보내는 편이 안전하다)
SECRET_QUERY = re.compile(r"(?<=[?&])(token=)([^&#\s]*)", re.IGNORECASE)


def mask_query(path):
    return SECRET_QUERY.sub(lambda m: m.group(1) + "***", path)


class MaskAccessLog(logging.Filter):
    """uvicorn.access 로그의 인자(클라이언트, 메서드, 주소, HTTP 버전, 상태)에서 주소의 token 값을 가린다."""

    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) >= 3 and isinstance(record.args[2], str):
            record.args = (*record.args[:2], mask_query(record.args[2]), *record.args[3:])
        return True


def mask_access_log():
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, MaskAccessLog) for f in logger.filters):
        logger.addFilter(MaskAccessLog())
