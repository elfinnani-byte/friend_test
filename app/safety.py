"""모델 판단과 상관없이 코드 규칙으로 처리하는 안전장치."""
import re

CRISIS_KEYWORDS = ["죽고 싶", "죽고싶", "자살", "살기 싫", "살기싫", "사라지고 싶", "자해", "목숨을 끊", "극단적 선택"]

CRISIS_REPLY = (
    "지금 많이 힘든 마음이 느껴져서 걱정돼요. 저는 AI라서 충분히 도와드릴 수 없지만, "
    "자살예방상담전화 109(24시간)나 정신건강위기상담전화 1577-0199에 이야기하면 바로 들어 줄 사람이 있어요. "
    "지금 위험한 상황이라면 112나 119에 연락해 주세요."
)

# 기억에 저장하지 않을 민감 정보
SENSITIVE_PATTERNS = [
    re.compile(r"\d{6}\s?-\s?[1-4]\d{6}"),                 # 주민등록번호
    re.compile(r"\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}"),    # 카드 번호
    re.compile(r"\d{2,6}-\d{2,6}-\d{2,8}"),                # 계좌 번호
    re.compile(r"비밀번호|패스워드|password", re.I),
]


def is_crisis(text):
    return any(k in text for k in CRISIS_KEYWORDS)


def is_sensitive(text):
    return any(p.search(text) for p in SENSITIVE_PATTERNS)
