"""퀴즈 시험지(tests/fixtures/quiz_eval/questions.json)를 실제 모델로 풀어 정답률과 평균 시간을 표로 보여 준다.

실행(직접 실행할 때만 LLM 을 부른다. 요금이 든다):
  python scripts/quiz_eval.py
  python scripts/quiz_eval.py --repeat 3 --only hard-code
  python scripts/quiz_eval.py --image-model claude-sonnet-5-5 --text-model claude-haiku-5-5

- 키와 제공자는 .env(또는 환경변수)의 LLM_PROVIDER, LLM_API_KEY 를 앱과 같은 방식으로 읽는다. 값은 출력하지 않는다.
- 모델은 --image-model / --text-model, 없으면 QUIZ_MODEL / QUIZ_MODEL_TEXT 를 쓴다.
- 풀이는 /api/quiz/solve 와 같은 흐름(app/quiz_flow.py)을 쓰고, 퀴즈 서버에는 제출하지 않는다.
- --server 주소를 주면 그 풀이 서버의 /api/quiz/solve 를 불러 잰다(모델은 그 서버의 설정을 따른다).
  예: --server https://eval-sonnet---friend-agent-staging-....run.app  (트래픽 0% 이름표 리비전)
  제출 정보(quiz_server, token)는 보내지 않으므로 퀴즈 서버에는 제출하지 않는다.
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


def remote_request(item, folder, eval_tag=None, allow_guess=False, supports_retake=False):
    """시험지 항목을 풀이 서버 요청 하나로 바꾼다. (json 또는 None, files 또는 None, data 또는 None)
    eval_tag 는 Langfuse metadata 의 eval 로, allow_guess 는 안 보여도 끝까지 풀게 하는 옵션으로,
    supports_retake 는 다시 찍기 응답을 받겠다는 표시로 보낸다."""
    extra = {**({"eval": eval_tag} if eval_tag else {}), **({"allow_guess": "true"} if allow_guess else {}),
             **({"supports_retake": "true"} if supports_retake else {})}
    if item["mode"] == "hard":
        path = folder / item["image"]
        kind = mimetypes.guess_type(item["image"])[0] or "image/png"
        data = {"type": item["type"], **({"choice_count": str(item["choice_count"])} if item.get("choice_count") else {}),
                **extra}
        return None, {"photo": (path.name, path.read_bytes(), kind)}, data
    if item.get("image"):
        raise SystemExit(f"{item['id']}: 이지모드 그림 문제는 --server 로 잴 수 없습니다(그림 주소가 필요).")
    return {"type": item["type"], "question": item["question"], "choices": item.get("choices"), **extra}, None, None


def run_remote(items, folder, server, repeat=1, pause=0.0, post=None, sleep=time.sleep, eval_tag=None, allow_guess=False,
               supports_retake=False):
    """풀이 서버를 불러 같은 문제를 repeat 번씩 푼다. 분당 요청 제한(429)이면 1분 기다렸다가 한 번 더 보낸다."""
    import httpx
    url = server.rstrip("/") + "/api/quiz/solve"
    post = post or httpx.Client(timeout=90).post
    rows = []
    for item in items:
        for n in range(repeat):
            body, files, data = remote_request(item, folder, eval_tag, allow_guess, supports_retake)
            started = time.perf_counter()
            for attempt in range(2):
                r = post(url, json=body) if body is not None else post(url, files=files, data=data)
                if r.status_code != 429 or attempt:
                    break
                sleep(61)
                started = time.perf_counter()
            elapsed = round((time.perf_counter() - started) * 1000)
            result = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            ok_status = r.status_code == 200
            retake = ok_status and result.get("retake") is True  # 풀이 서버가 다시 찍기를 알렸다(답 없음, 제출 안 함)
            rows.append({"id": item["id"], "try": n + 1, "mode": item["mode"], "type": item["type"],
                         "label": item.get("label"), "problem": item.get("problem"),
                         "retake": retake, "missing": result.get("missing") if retake else None,
                         "model": server.split("//")[-1].split("---")[0] if "---" in server else "server",
                         "expected": item["answer"], "got": result.get("choice", result.get("text")),
                         "ok": ok_status and not retake and correct(item, result), "reason": result.get("reason", ""),
                         "timing": {**(result.get("timing") or {}), "round_trip": elapsed},
                         "notes": [], "error": None if ok_status else f"HTTP {r.status_code}"})
            if pause:
                sleep(pause)
    return rows


def table(rows):
    lines = ["| 문제 | 회 | 모드 | 모델 | 정답 | 모델 답 | 맞음 | 첫 토큰 | 답 확정 | 전체 | 왕복 | 근거 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    sec = lambda ms: f"{ms / 1000:.2f}초" if isinstance(ms, (int, float)) else "-"
    for r in rows:
        t = r["timing"]
        got = r["error"] or ("다시 찍기: " + (r.get("missing") or "") if r.get("retake") else r["got"])
        lines.append(f"| {r['id']} | {r['try']} | {r['mode']} | {r['model']} | {r['expected']} | {got} | "
                     f"{'O' if r['ok'] else 'X'} | {sec(t.get('first_token'))} | {sec(t.get('answer'))} | "
                     f"{sec(t.get('total'))} | {sec(t.get('round_trip'))} | {r['reason']} |")
    return "\n".join(lines)


def summary(rows):
    if not rows:
        return "풀 문제가 없습니다."
    total = [r["timing"]["total"] for r in rows if "total" in r["timing"]]
    answer = [r["timing"]["answer"] for r in rows if "answer" in r["timing"]]
    first = [r["timing"]["first_token"] for r in rows if "first_token" in r["timing"]]
    out = [f"정답률 {sum(r['ok'] for r in rows)}/{len(rows)} ({100 * sum(r['ok'] for r in rows) / len(rows):.0f}%), "
           f"평균 첫 토큰 {sum(first) / max(1, len(first)) / 1000:.2f}초, "
           f"평균 답 확정 {sum(answer) / max(1, len(answer)) / 1000:.2f}초, 평균 전체 {sum(total) / max(1, len(total)) / 1000:.2f}초"]
    retakes = sum(1 for r in rows if r.get("retake"))
    if retakes:
        out.append(f"- 다시 찍기 {retakes}번(정답 수에 넣지 않음)")
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
    parser.add_argument("--server", help="이 풀이 서버의 /api/quiz/solve 로 잰다(제출 정보는 보내지 않음)")
    parser.add_argument("--pause", type=float, default=0.0, help="--server 일 때 요청 사이 쉬는 초(분당 제한 대비)")
    parser.add_argument("--eval", dest="eval_tag", help="--server 요청에 붙일 측정 표시(Langfuse metadata eval)")
    parser.add_argument("--allow-guess", action="store_true", help="--server 요청에 allow_guess=true(안 보여도 끝까지 풀기)")
    parser.add_argument("--supports-retake", action="store_true",
                        help="--server 요청에 supports_retake=true(안 보이면 다시 찍기 응답을 받음)")
    args = parser.parse_args(argv)

    if args.server:
        items = load_items(args.dir, args.only)
        print(f"[풀이 서버 {args.server}, {len(items)}문제 x {args.repeat}회, 퀴즈 서버에는 제출하지 않음]")
        rows = run_remote(items, args.dir, args.server, args.repeat, args.pause, eval_tag=args.eval_tag,
                          allow_guess=args.allow_guess, supports_retake=args.supports_retake)
        print(table(rows))
        print()
        print(summary(rows))
        return rows

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
