"""로그 한 줄을 JSON으로 남긴다.

Cloud Run은 JSON 로그의 severity 값을 읽어 로그 화면의 심각도(정보, 경고, 오류)로 보여 준다.
키나 문의 원문 같은 값은 로그에 남기지 않는다.
"""
import json
import sys


def log(severity, message, **fields):
    record = {"severity": severity, "message": message, **fields}
    print(json.dumps(record, ensure_ascii=False), file=sys.stdout, flush=True)
