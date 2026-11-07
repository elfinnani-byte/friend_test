"""퀴즈 시험지(tests/fixtures/quiz_eval/questions.json)를 실제 모델로 풀어 정답률과 평균 시간을 표로 보여 준다.

실행(직접 실행할 때만 LLM 을 부른다. 요금이 든다):
  python scripts/quiz_eval.py
  python scripts/quiz_eval.py --repeat 3 --only hard-code
  python scripts/quiz_eval.py --image-model claude-sonnet-5-5 --text-model claude-haiku-5-5

- 키와 제공자는 .env(또는 환경변수)의 LLM_PROVIDER, LLM_API_KEY 를 앱과 같은 방식으로 읽는다. 값은 출력하지 않는다.
- 모델은 --image-model / --text-model, 없으면 QUIZ_MODEL / QUIZ_MODEL_TEXT 를 쓴다.
- 풀이는 /api/quiz/solve 와 같은 흐름(app/quiz_flow.py)을 쓰고, 퀴즈 서버에는 제출하지 않는다.
- 형식은 tests/fixtures/quiz_eval/README.md 를 본다.
"""
import argparse
import json
import mimetypes
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import quiz, quiz_flow  # noqa: E402

DEFAULT_DIR = ROOT / "tests" / "fixtures" / "quiz_eval"


def load_items(folder, only=None):
    path = folder / "questions.json"
    if not path.exists():
        sys.exit(f"{path} 가 없습니다. 같은 폴더의 questions.example.json 을 복사해 문제를 넣으세요.")
    items = json.loads(path.read_text(encoding="utf-8"))
    if only:
        items = [i for i in items if i["id"] in only]
    return items


def build(item, folder):
    """시험지 항목 하나를 풀이 입력(글, 그림, 보기 수, 트레이스)으로 바꾼다."""
    image = None
    if item.get("image"):
        data = (folder / item["image"]).read_bytes()
        image = {"data": data, "media_type": mimetypes.guess_type(item["image"])[0] or "image/png"}
    if item["mode"] == "hard":
        return quiz.hard_request(item["type"], item.get("choice_count"), image["data"], image["media_type"])
    text, _, count, trace = quiz.easy_request({"type": item["type"], "question": item["question"],
                                               "choices": item.get("choices"),
                                               "image_url": "https://quiz-eval.local/image" if image else None})
    return text, image, count, trace  # 그림은 주소 대신 바이트로 보낸다(안내 문장은 easy_request 가 넣었다)


def same_text(a, b):
    norm = lambda t: re.sub(r"[\s\W_]+", "", str(t)).lower()  # 띄어쓰기·대소문자·문장 부호를 빼고 비교
    return norm(a) == norm(b)


def correct(item, result):
    if item["type"] == "choice":
        return result.get("choice") == item["answer"]
    return any(same_text(result.get("text", ""), a) for a in [item["answer"], *item.get("answers", [])])


def run(items, folder, image_model, text_model, repeat=1):
    rows = []
    for item in items:
        for n in range(repeat):
            text, image, count, trace = build(item, folder)
            model = quiz_flow.pick_models(image, image_model, text_model)
            started = time.perf_counter()
            try:
                done = quiz_flow.solve(model, text, image, count, trace, choices=trace.get("choices"),
                                       question=trace.get("question"), started=started)
                result, timing, notes, error = done["result"], done["timing"], done["notes"], None
            except Exception as exc:
                result, timing, notes, error = {}, {"total": quiz_flow.elapsed_ms(started)}, [], type(exc).__name__
            rows.append({"id": item["id"], "try": n + 1, "mode": item["mode"], "type": item["type"], "model": model.model,
                         "expected": item["answer"], "got": result.get("choice", result.get("text")),
                         "ok": bool(result) and correct(item, result), "reason": result.get("reason", ""),
                         "timing": timing, "notes": notes, "error": error})
    return rows


def table(rows):
    lines = ["| 문제 | 회 | 모드 | 모델 | 정답 | 모델 답 | 맞음 | 첫 토큰 | 답 확정 | 전체 | 근거 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    sec = lambda ms: f"{ms / 1000:.2f}초" if isinstance(ms, (int, float)) else "-"
    for r in rows:
        t = r["timing"]
        got = r["error"] or r["got"]
        lines.append(f"| {r['id']} | {r['try']} | {r['mode']} | {r['model']} | {r['expected']} | {got} | "
                     f"{'O' if r['ok'] else 'X'} | {sec(t.get('first_token'))} | {sec(t.get('answer'))} | "
                     f"{sec(t.get('total'))} | {r['reason']} |")
    return "\n".join(lines)


def summary(rows):
    if not rows:
        return "풀 문제가 없습니다."
    total = [r["timing"]["total"] for r in rows if "total" in r["timing"]]
    answer = [r["timing"]["answer"] for r in rows if "answer" in r["timing"]]
    out = [f"정답률 {sum(r['ok'] for r in rows)}/{len(rows)} ({100 * sum(r['ok'] for r in rows) / len(rows):.0f}%), "
           f"평균 답 확정 {sum(answer) / max(1, len(answer)) / 1000:.2f}초, 평균 전체 {sum(total) / max(1, len(total)) / 1000:.2f}초"]
    for mode in sorted({r["mode"] for r in rows}):
        part = [r for r in rows if r["mode"] == mode]
        out.append(f"- {mode}: 정답 {sum(r['ok'] for r in part)}/{len(part)}, "
                   f"평균 전체 {sum(r['timing'].get('total', 0) for r in part) / len(part) / 1000:.2f}초")
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description="퀴즈 시험지를 실제 모델로 풀어 정답률과 시간을 본다")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--image-model")
    parser.add_argument("--text-model")
    args = parser.parse_args(argv)

    from app.config import load_settings
    from app.llm import make_llm

    settings = load_settings()  # .env 를 읽는다(값은 출력하지 않는다)
    image_name = args.image_model or settings.quiz_model
    text_name = args.text_model or settings.quiz_model_text or None
    image_model = make_llm(settings, image_name)
    text_model = make_llm(settings, text_name) if text_name else None
    items = load_items(args.dir, args.only)
    print(f"[그림 문제 모델 {image_model.model}, 글 문제 모델 {(text_model or image_model).model}, "
          f"{len(items)}문제 x {args.repeat}회]")
    rows = run(items, args.dir, image_model, text_model, args.repeat)
    print(table(rows))
    print()
    print(summary(rows))
    return rows


if __name__ == "__main__":
    main()
