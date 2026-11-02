"""Langfuse 계측. 메시지 한 턴이 트레이스 하나(chat-turn), 먼저 말 걸기가 트레이스 하나(greeting)다.

관찰 단위
  chat-turn(트레이스)  safety(span)  choose-tools(generation)  tool:<도구 이름>(span)
  reply(generation)    memory-update(generation)  memory-summary(generation)
  greeting(트레이스, generation)

- 입력과 출력 원문을 Langfuse로 보낸다. 보내기 직전에 전화번호, 이메일, 카드 번호 패턴만 가린다(mask_pii).
  서버 로그(app/logs.py)에는 지금처럼 원문을 남기지 않는다.
- LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY 가 없으면 계측을 끈다. 꺼져 있어도 아래 함수는 그대로 불러도 되고,
  아무것도 보내지 않는다.

근거 문서(Python SDK v5):
  https://langfuse.com/docs/observability/sdk/instrumentation
  https://langfuse.com/docs/observability/features/masking
  https://langfuse.com/docs/observability/features/token-and-cost-tracking
"""
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from langfuse import Langfuse, propagate_attributes
from langfuse.types import MaskOtelSpansResult, OtelSpanPatch
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from app.logs import log

# 원문이 들어가는 속성. 이 속성의 문자열에서만 개인정보 패턴을 찾는다(ID, 시각 같은 다른 속성은 건드리지 않는다).
MASKED_PREFIXES = ("langfuse.observation.input", "langfuse.observation.output", "langfuse.observation.metadata.",
                   "langfuse.trace.metadata.", "gen_ai.")
PII_PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[이메일]"),
    (re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"), "[카드번호]"),  # 13~19자리, 공백·하이픈 허용
    (re.compile(r"(?<!\d)(?:\+82[ -]?|0)(?:1[016789]|2|[3-6]\d|70)[ .-]?\d{3,4}[ .-]?\d{4}(?!\d)"), "[전화번호]"),
]

KST = ZoneInfo("Asia/Seoul")

_client = None
_enabled = False  # setup 에서 키가 있어 계측을 켰는지
_provider = None  # setup 에서 만든 tracer provider. flush 가 이것을 비운다
_version = "local"  # Cloud Run 리비전(K_REVISION). setup 에서 정한다
_commit = "local"   # COMMIT_SHA


def mask_text(text):
    for pattern, label in PII_PATTERNS:
        text = pattern.sub(label, text)
    return text


def mask_data(data):
    if isinstance(data, str):
        return mask_text(data)
    if isinstance(data, list):
        return [mask_data(item) for item in data]
    if isinstance(data, dict):
        return {key: mask_data(value) for key, value in data.items()}
    return data


def mask_value(value):
    """속성 값 하나를 가린다. 메시지 목록 같은 값은 JSON 문자열로 들어오고 한글이 \\uXXXX 로 바뀌어 있어서,
    그대로 정규식에 넣으면 이스케이프의 숫자와 전화번호가 이어져 카드 번호로 잡히고 JSON이 깨진다.
    그래서 JSON이면 풀어서 안의 문자열만 가린 뒤 다시 묶는다."""
    try:
        data = json.loads(value)
    except ValueError:
        return mask_text(value)
    masked = mask_data(data)
    return value if masked == data else json.dumps(masked, ensure_ascii=False)


def mask_pii(*, params):
    """내보내기 직전에 원문 속성에서 전화번호, 이메일, 카드 번호를 가린다."""
    patches = {}
    for identifier, span in params.spans.items():
        changed = {}
        for key, value in span.attributes.items():
            if isinstance(value, str) and key.startswith(MASKED_PREFIXES):
                masked = mask_value(value)
                if masked != value:
                    changed[key] = masked
        if changed:
            patches[identifier] = OtelSpanPatch(set_attributes=changed)
    return MaskOtelSpansResult(span_patches=patches)


def setup(settings):
    """앱 시작 때 한 번 부른다. .env 를 읽은 뒤(load_settings 뒤)에 불러야 키를 찾는다.
    environment 는 서비스 단위 값이라 클라이언트에 넣는다(ENV: local, staging, production)."""
    global _client, _enabled, _provider, _version, _commit
    _version, _commit = settings.revision, settings.commit
    enabled = bool(os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip() and os.environ.get("LANGFUSE_SECRET_KEY", "").strip())
    if enabled:
        provider = _provider = make_provider()
        if isinstance(otel_trace.get_tracer_provider(), otel_trace.ProxyTracerProvider):
            otel_trace.set_tracer_provider(provider)  # FastAPI 요청 span 도 같은 provider 를 쓰게 한다
        _client = Langfuse(environment=settings.env, mask_otel_spans=mask_pii, tracer_provider=provider)
    else:
        _client = disabled_client()
    _enabled = enabled
    log("INFO", "Langfuse 계측", enabled=enabled)
    return _client


class Attributes:
    """트레이스 하나에 붙일 속성(사용자, 세션, 버전, metadata, tags).

    propagate_attributes 는 감싼 구간 안에서 새로 만드는 관찰에만 붙는다. 그래서 관찰을 만드는 곳마다
    scope() 로 감싼다(yield 가 없는 구간에서만). 태그는 관찰을 만들 때만 붙일 수 있고, 트레이스의 모든
    관찰 태그가 모여 트레이스 태그가 된다. https://langfuse.com/docs/observability/features/tags
    """

    def __init__(self, user_id):
        self.user_id = str(user_id)  # 서버가 로그인으로 확인한 사용자 ID. 이메일은 넣지 않는다
        self.session_id = f"{self.user_id}-{datetime.now(KST):%Y-%m-%d}"  # 트레이스를 열 때 한 번 정한다
        self.metadata = {"commit": _commit}
        self.tags = []

    def add(self, metadata=None, tags=()):
        """이후에 만드는 관찰부터 붙는다. None 값은 Langfuse가 버리므로 넣지 않는다."""
        self.metadata.update({k: v for k, v in (metadata or {}).items() if v is not None})
        self.tags += [t for t in tags if t not in self.tags]

    @contextmanager
    def scope(self):
        with propagate_attributes(user_id=self.user_id, session_id=self.session_id, version=_version,
                                  metadata=dict(self.metadata), tags=list(self.tags) or None):
            yield


def make_provider():
    """모든 트레이스를 기록하는 tracer provider.

    Cloud Run 앞단은 요청마다 traceparent 헤더를 붙이고 일부에만 '샘플함' 표시를 켠다. FastAPI 가 그 헤더를 이어받고,
    OpenTelemetry 기본 샘플러 ParentBased(ALWAYS_ON) 는 원격 부모가 '샘플 안 함'이면 기록하지 않아 트레이스가 빠졌다.
    ALWAYS_ON 은 부모의 표시와 상관없이 기록한다. 트레이스 ID 는 부모를 이어받으므로 Cloud Run trace ID 와 같다.
    https://langfuse.com/docs/observability/sdk/advanced-features (Langfuse respects OpenTelemetry's sampling decisions)
    """
    return TracerProvider(sampler=ALWAYS_ON)


def disabled_client():
    # 키 없이 만들면 SDK가 인증 오류 경고를 남기므로, 보내지 않는 클라이언트임을 밝혀 만든다
    return Langfuse(public_key="disabled", secret_key="disabled", tracing_enabled=False)


def client():
    global _client
    if _client is None:  # setup 전에 불리면(단위 테스트 등) 아무것도 보내지 않는다
        _client = disabled_client()
    return _client


@contextmanager
def observe(name, as_type="span", parent=None, **fields):
    """yield 가 없는 구간 하나를 관찰로 감싼다. parent 가 없으면 지금 열려 있는 관찰 아래에 붙는다."""
    opener = parent.start_as_current_observation if parent is not None else client().start_as_current_observation
    with opener(name=name, as_type=as_type, **fields) as observation:
        yield observation


def start(name, as_type="span", parent=None, **fields):
    """스트리밍처럼 yield 를 넘나드는 구간용. StreamingResponse 는 조각마다 다른 문맥에서 실행하므로
    with 문 대신 객체를 들고 다니다가 end() 를 부른다."""
    opener = parent.start_observation if parent is not None else client().start_observation
    return opener(name=name, as_type=as_type, **fields)


def record(observation=None, *, model, input=None, output=None, input_tokens=None, output_tokens=None):
    """세 LLM 클래스가 같은 방식으로 generation 을 기록한다. observation 이 없으면 지금 열린 generation 에 적는다."""
    fields = {"model": model}
    if input is not None:
        fields["input"] = input
    if output is not None:
        fields["output"] = output
    usage = {k: v for k, v in (("input", input_tokens), ("output", output_tokens)) if v is not None}
    if usage:
        fields["usage_details"] = usage
    if observation is None:
        client().update_current_generation(**fields)
    else:
        observation.update(**fields)


def mark_first_token(observation):
    if observation is not None:
        observation.update(completion_start_time=datetime.now(timezone.utc))


def flush():
    """Cloud Run 은 응답이 끝나면 CPU를 거의 주지 않아서, 응답을 닫기 전에 쌓인 관찰을 보낸다.

    계측이 꺼져 있으면 아무것도 하지 않는다. 켜져 있으면 걸린 시간(ms)과 성공 여부(ok)만 로그에 남긴다.
    Langfuse.flush() 는 성공 여부를 돌려주지 않아서, setup 에서 만든 provider 의 force_flush() 를 직접 부른다.
    실패해도 응답을 깨지 않도록 예외는 ok=false 로만 남긴다(오류 내용은 남기지 않는다).
    """
    if not _enabled:
        return
    started = time.perf_counter()
    ok = False
    try:
        ok = bool((_provider or otel_trace.get_tracer_provider()).force_flush())
        client().flush()  # 점수(create_score)는 따로 쌓이므로 그 대기열도 보낸다
    except Exception:
        ok = False
    log("INFO", "Langfuse flush", ms=round((time.perf_counter() - started) * 1000), ok=ok)


def trace_id(observation):
    """계측이 켜져 있을 때만 관찰의 트레이스 ID. 메시지 meta 에 저장해 두었다가 사용자 반응을 그 트레이스에 잇는다."""
    return observation.trace_id if _enabled else None


def link(meta, observation):
    """답 메시지 meta 에 트레이스 ID 를 더한다. 계측이 꺼져 있으면 그대로 둔다."""
    trace = trace_id(observation)
    return {**meta, "trace_id": trace} if trace else meta


def score(name, value, trace, comment=None):
    """BOOLEAN 점수(0 또는 1) 하나를 트레이스에 붙여 보낸다. 보냈으면 True.

    계측이 꺼져 있거나 트레이스 ID 가 없으면 건너뛴다. 실패해도 응답을 깨지 않는다.
    score_id 를 트레이스와 이름으로 고정해, 같은 답에 다시 누르면 새로 쌓지 않고 덮어쓴다.
    comment 는 사용자가 쓴 문장일 수 있어 전화번호, 이메일, 카드 번호를 가리고 500자로 자른다.
    https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk
    """
    if not _enabled or not trace:
        return False
    try:
        client().create_score(name=name, value=value, trace_id=trace, score_id=f"{trace}-{name}",
                              data_type="BOOLEAN", comment=mask_text(comment)[:500] if comment else None)
    except Exception:
        return False
    flush()
    return True
