"""도구 선택 평가. data/eval/tool_selection.jsonl 의 메시지마다 고른 도구와 핵심 인자가 정답과 맞는지 센다.

실행:
  python scripts/eval_tools.py            # .env 의 LLM 으로 모델 선택기를 평가 (호출 40번, 비용 발생)
  python scripts/eval_tools.py --rule     # 키워드 규칙 선택기를 평가 (비용 없음)
  python scripts/eval_tools.py --label staging     # Langfuse 프롬프트 router 의 staging 라벨 버전으로 평가
  python scripts/eval_tools.py --label production  # production 라벨 버전으로 평가
  python scripts/eval_tools.py --show-raw          # 틀린 항목에 모델이 돌려준 JSON 을 함께 출력

--label 이 없으면 코드의 문장(app/router.py 의 ROUTER_TEMPLATE)으로 평가한다.
--label 의 프롬프트를 가져오지 못하면 코드의 문장으로 바꾸지 않고 멈춘다(다른 문장을 채점하지 않으려고).

정답의 날짜와 시각은 '2026-10-12(월) 14:00 한국 시간'을 지금으로 보고 만들었다. 평가도 그 시각을 지금으로 쓴다.

다음 공휴일을 묻는 항목(next_holiday 가 있는 항목)은 get_holidays 의 기간이 그 공휴일을 포함하는지도 본다.
from, to 가 비어 있으면 도구가 오늘부터 1년을 찾으므로 통과로 센다.
"""
import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_dotenv, load_settings  # noqa: E402
from app.llm import make_llm  # noqa: E402
from app.router import PROMPT_NAME, model_choose, rule_choose  # noqa: E402

NOW = datetime.fromisoformat("2026-10-12T14:00:00+09:00")
LOCATION = "서울"


def args_match(expected, actual):
    """정답에 적힌 인자만 비교한다. due_at 은 분 단위까지 같은 시각이면 맞다."""
    wrong = []
    for key, want in expected.items():
        got = actual.get(key)
        if key == "due_at":
            try:
                same = datetime.fromisoformat(str(got)).replace(tzinfo=None, second=0) == datetime.fromisoformat(want)
            except ValueError:
                same = False
        elif key in ("from", "to"):
            same = str(got or "")[:7] == want[:7]   # 월까지만 비교
        elif key == "day":   # 정답은 '오늘로부터 며칠 뒤', 결과는 date(YYYY-MM-DD)
            try:
                same = (datetime.fromisoformat(str(actual.get("date"))).date() - NOW.date()).days == want
            except ValueError:
                same = False
            got = actual.get("date")
        else:
            same = want in str(got or "")
        if not same:
            wrong.append(f"{key}: 정답 {want}, 결과 {got}")
    return wrong


def holiday_in_range(next_holiday, args):
    """get_holidays 의 기간(from~to)이 다음 공휴일을 포함하는지. 맞으면 빈 목록, 아니면 실패 설명 한 줄.

    비어 있는 쪽은 검사하지 않는다. 도구는 빈 from 을 오늘, 빈 to 를 오늘부터 1년 뒤로 채운다(app/tools.py get_holidays).
    날짜로 읽을 수 없는 값은 포함 여부를 알 수 없으므로 실패로 센다."""
    start, end = args.get("from"), args.get("to")
    target = date.fromisoformat(next_holiday)
    try:
        inside = ((not start or date.fromisoformat(str(start)) <= target)
                  and (not end or target <= date.fromisoformat(str(end))))
    except ValueError:
        inside = False
    if inside:
        return []
    return [f"정답 기간에 {next_holiday} 포함, 결과 {start or '(없음)'}~{end or '(없음)'}"]


def score_row(row, choice):
    """한 항목을 채점한다. (도구가 맞았는지, 틀린 인자 설명 목록)"""
    picked = {t["name"] for t in choice.tools}
    required, optional = set(row["expected_tools"]), set(row["optional_tools"])
    tools_right = required <= picked <= required | optional
    wrong = []
    if tools_right:
        got = {t["name"]: t["args"] for t in choice.tools}
        for name, expected in row["expected_args"].items():
            wrong += args_match(expected, got.get(name, {}))
        if row.get("next_holiday"):
            wrong += holiday_in_range(row["next_holiday"], got.get("get_holidays", {}))
    return tools_right, wrong


class RawRecorder:
    """llm 을 감싸, complete_json 이 돌려준 모델 응답을 코드가 고치기 전에 JSON 문자열로 남긴다(--show-raw).

    model_choose 는 받은 args 를 그 자리에서 고치므로(연도 고침), 돌려받자마자 문자열로 바꿔 둔다.
    파싱한 결과를 다시 쓴 것이라 띄어쓰기는 모델이 보낸 글과 다를 수 있다. 출력하는 것은 이 응답뿐이다."""

    def __init__(self, llm):
        self.llm = llm
        self.last = None

    def __getattr__(self, name):  # model, can_reason 등은 감싼 llm 의 것을 쓴다
        return getattr(self.llm, name)

    def complete_json(self, system, user):
        self.last = None
        data = self.llm.complete_json(system, user)
        self.last = json.dumps(data, ensure_ascii=False)
        return data


def fetch_prompt(label):
    """Langfuse 에서 router 프롬프트의 label 버전을 가져온다. 트레이스는 보내지 않는다. 키 값은 출력하지 않는다."""
    load_dotenv(ROOT / ".env")
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        sys.exit("LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY 가 없어 --label 프롬프트를 가져올 수 없습니다.")
    from langfuse import Langfuse

    try:
        return Langfuse(tracing_enabled=False).get_prompt(PROMPT_NAME, label=label, type="text", cache_ttl_seconds=0)
    except Exception as error:
        sys.exit(f"Langfuse 프롬프트 {PROMPT_NAME}({label})를 가져오지 못했습니다: {type(error).__name__}")


def main():
    parser = argparse.ArgumentParser(description="도구 선택 평가")
    parser.add_argument("--rule", action="store_true", help="키워드 규칙 선택기를 평가한다(비용 없음)")
    parser.add_argument("--label", help="Langfuse 프롬프트 router 의 이 라벨 버전으로 평가한다(예: staging, production)")
    parser.add_argument("--show-raw", action="store_true", help="틀린 항목에 모델이 돌려준 JSON(코드가 고치기 전)을 함께 출력한다")
    options = parser.parse_args()
    use_rule = options.rule
    if use_rule and (options.label or options.show_raw):
        sys.exit("--rule 은 모델을 쓰지 않아 --label, --show-raw 와 함께 쓸 수 없습니다.")
    llm = None if use_rule else make_llm(load_settings())
    if llm is not None and not llm.can_reason:
        print("LLM_PROVIDER=fake 에서는 모델 평가를 할 수 없습니다. --rule 로 실행하세요.")
        return
    if options.show_raw:
        llm = RawRecorder(llm)
    prompt = fetch_prompt(options.label) if options.label else None
    prompt_name = f"{PROMPT_NAME} v{prompt.version}({options.label})" if prompt else "코드의 문장"

    rows = [json.loads(line) for line in (ROOT / "data/eval/tool_selection.jsonl").read_text(encoding="utf-8").splitlines()]
    tool_ok = args_ok = 0
    failures = []
    for row in rows:
        if use_rule:
            choice = rule_choose(row["message"], NOW, LOCATION)
        else:
            choice = model_choose(row["message"], row["history"], NOW, LOCATION, llm, prompt=prompt)
        picked = {t["name"] for t in choice.tools}
        tools_right, wrong = score_row(row, choice)
        if tools_right:
            tool_ok += 1
            if not wrong:
                args_ok += 1
        if not tools_right or wrong:
            failures.append((row, sorted(picked), wrong, choice.confidence, llm.last if options.show_raw else None))

    n = len(rows)
    print(f"[{'키워드 규칙' if use_rule else f'모델 {llm.model}, 프롬프트 {prompt_name}'}] 도구 정답 {tool_ok}/{n}, 도구와 인자 모두 정답 {args_ok}/{n}")
    for row, picked, wrong, conf, raw in failures:
        detail = "; ".join(wrong) if wrong else f"정답 {row['expected_tools'] or '도구 없음'}, 결과 {picked or '도구 없음'}"
        print(f"  {row['id']} {row['message']}  →  {detail}  (확신도 {conf:.2f})")
        if options.show_raw:
            print(f"      모델 응답: {raw}")


if __name__ == "__main__":
    main()
