"""퀴즈 한 문제 풀기 흐름(스트리밍). /api/quiz/solve, 서버 직접 풀기(auto), 시험지 스크립트가 함께 쓴다.

- 글 문제(그림 없음): QUIZ_MODEL_TEXT 로 "ANSWER: …" → "REASON: …" 두 줄을 받는다.
- 그림 문제(이지모드 그림, 하드모드 사진): QUIZ_MODEL 로 "SEEN" → "VISIBLE" → "THINK" → "ANSWER" → "REASON" 을 받는다.
  SEEN, VISIBLE, THINK 는 퀴즈 서버에 보내지 않고 로그에만 남긴다.
- 하드모드 사진에서 VISIBLE: no(답에 필요한 그림·표·보기가 잘리거나 읽을 수 없음)면 그 자리에서 멈추고 제출하지 않는다.
  응답은 {"retake": true, "missing": "안 보이는 부분"} — 앱이 다시 찍어 allow_guess=true 로 보내면 끝까지 풀어 제출한다.
- REASON 줄이 끝나는 즉시(줄바꿈이 없으면 근거가 20자에 닿는 순간) 답과 근거를 /api/answer 에 한 번 제출한다.
  퀴즈 서버에는 근거만 따로 받는 엔드포인트가 없고, 같은 문제에 두 번 내면 409 라서 한 번에 낸다.
- 객관식 번호를 못 읽으면 번호만 한 번 더 묻고, 그래도 없으면 1번을 낸다(답을 안 내면 0점).
- 단계별 시간(요청 도착 기준 ms): first_token, answer, submitted, reason_sent(= submitted, 한 번에 내므로)
"""
import re
import time

from app import quiz, tracing
from app.config import QUIZ_MAX_TOKENS, QUIZ_MAX_TOKENS_IMAGE, QUIZ_MAX_TOKENS_TEXT, QUIZ_REASON_MAX
from app.logs import log

_COMMON = [
    "- ANSWER: 객관식이면 '번호 (고른 보기 글)' 모양으로 쓴다. 번호는 1부터 센다(화면 사진이면 화면에 적힌 번호 그대로).",
    "  보기 글은 적힌 그대로 옮긴다. 예: ANSWER: 2 (200) / ANSWER: 3 ([10, 30, 50])",
    "  단답이면 답만 쓴다. 문제에 형식 지시('숫자만', '한 단어로', '단위 포함' 등)가 있으면 반드시 그 형식만 쓴다.",
    "  '숫자만'이면 단위·조사 없이 숫자만('7개'가 아니라 '7'). 지시가 없으면 가장 짧은 답. 따옴표·설명은 붙이지 않는다.",
    "- REASON: 답을 고른 근거를 3~4단어 명사구로 쓴다. 문장으로 끝내지 않는다('~다', '~요', '~때문' 로 끝내지 않음).",
    "  예: 200은 성공 코드 / y가 x를 참조 / 빨간 원 7개 / 막대가 가장 긴 B",
    "  문제 종류 이름('상식:', '코드:' 같은 머리말)은 붙이지 않는다.",
    "- 각 항목은 줄 맨 앞에 이름과 콜론을 붙인다. 마크다운, 빈 줄, 다른 말은 쓰지 않는다.",
]
TEXT_SYSTEM = "\n".join([
    "너는 퀴즈 대결에서 문제를 푸는 선수다. 문제를 읽고 정답 하나를 고른다.",
    "아래 두 줄만 이 순서로 출력한다.",
    "ANSWER: 답",
    "REASON: 근거",
    *_COMMON,
])
IMAGE_SYSTEM = "\n".join([
    "너는 퀴즈 대결에서 문제를 푸는 선수다. 그림(또는 퀴즈 화면 사진)을 보고 정답 하나를 고른다.",
    "아래 다섯 항목을 이 순서로 출력한다.",
    "SEEN: 그림 속 문제 글, 보기, 코드, 숫자를 보이는 그대로 옮겨 적는다. 여러 줄이어도 된다.",
    "VISIBLE: 답을 구하는 데 필요한 그림·표·보기가 사진에 모두 보이면 yes, 일부라도 잘리거나 읽을 수 없으면 no",
    "THINK: 두세 줄로 생각한다.",
    "ANSWER: 답",
    "REASON: 근거",
    "- SEEN 은 해석하지 말고 보이는 글자를 그대로 옮긴다. 코드는 줄바꿈과 들여쓰기를 지킨다.",
    "- VISIBLE 은 yes 또는 no 한 단어로 쓰고, no 이면 괄호 안에 안 보이는 부분을 짧게 쓴다. 예: VISIBLE: no (표 아래 두 줄 잘림)",
    "- 안 보이는 부분을 짐작해서 채우지 않는다. 보이는 것만으로 답이 정해지지 않으면 no 다.",
    *_COMMON,
])

KEYS = ("SEEN", "VISIBLE", "THINK", "ANSWER", "REASON")
_KEY_LINE = re.compile(r"^[\s*#>_`-]*(SEEN|VISIBLE|THINK|ANSWER|REASON)[\s*_`]*[:：][\s*_`]*(.*)$", re.IGNORECASE)


class QuizLines:
    """스트리밍 글 조각을 받아 SEEN/THINK/ANSWER/REASON 항목으로 나눈다. 이름 없는 줄은 앞 항목에 붙는다."""

    def __init__(self, reason_limit=QUIZ_REASON_MAX):
        self.buffer, self.current = "", None
        self.sections = {k: [] for k in KEYS}
        self.visible_done = False       # VISIBLE 줄이 줄바꿈으로 끝났다
        self.answer_done = False        # ANSWER 줄이 줄바꿈으로 끝났다
        self.reason_cut = False         # 근거가 상한에 닿아 줄이 끝나기 전에 잘랐다
        self.reason_done = False        # REASON 이 줄바꿈으로 끝났거나 글자 수 상한에 닿았다
        self.reason_limit = reason_limit

    def _line(self, line):
        """끝난 줄 하나를 항목에 넣는다."""
        match = _KEY_LINE.match(line)
        if match:
            self.current = match.group(1).upper()
            line = match.group(2)
        if self.current is None:
            return  # 첫 항목 이름이 나오기 전의 글은 버린다
        self.sections[self.current].append(line)
        if self.current == "VISIBLE" and self.text("VISIBLE"):
            self.visible_done = True
        if self.current == "ANSWER" and self.text("ANSWER"):
            self.answer_done = True
        if self.current == "REASON" and self.text("REASON"):
            self.reason_done = True

    def feed(self, chunk):
        self.buffer += chunk
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self._line(line)
        # 줄바꿈이 아직 없어도 REASON 이 상한에 닿으면 끝난 것으로 본다
        match = _KEY_LINE.match(self.buffer)
        pending = (match.group(2) if match and match.group(1).upper() == "REASON" else
                   self.buffer if self.current == "REASON" and not match else None)
        if pending is not None and len(clean(" ".join(self.sections["REASON"] + [pending]))) >= self.reason_limit:
            self._line(self.buffer)
            self.buffer = ""
            self.reason_cut = True  # 줄이 끝나기 전에 상한에 닿았다(모델이 근거를 더 길게 쓰던 중)

    def finish(self):
        if self.buffer:
            self._line(self.buffer)
            self.buffer = ""
        if self.text("ANSWER"):
            self.answer_done = True

    def text(self, key):
        return clean(" ".join(v for v in self.sections[key] if v.strip())) if key != "SEEN" else \
            "\n".join(self.sections["SEEN"]).strip()


def clean(text):
    return str(text).strip().strip('"\'*` ').strip()


_MISSING = re.compile(r"[(（]\s*(.+?)\s*[)）]")


def parse_visible(text):
    """VISIBLE 값 읽기. (True/False/None, 안 보이는 부분). "no (표 아래 잘림)", "no - 표 아래 잘림", "아니오" 모두 no."""
    value = clean(text or "")
    word = re.match(r"[A-Za-z가-힣]+", value)  # 첫 단어(뒤에 괄호가 붙어 있어도 떼어 읽는다)
    head = word.group(0).lower() if word else ""
    if head in ("yes", "y", "예", "네", "true"):
        return True, ""
    if head in ("no", "n", "아니오", "아니요", "false"):
        found = _MISSING.search(value)
        rest = found.group(1) if found else value[len(head):].strip(" -:—()")
        return False, quiz.clip_reason(rest, 40)
    return None, ""


def pick_models(image, image_model, text_model):
    """그림이 있으면 그림 모델, 없으면 글 모델(없으면 그림 모델)."""
    return image_model if image is not None or text_model is None else text_model


def app_timings(timing):
    """앱(mini-games)이 읽는 이름의 시간 칸(ms 정수). 앱은 timings 의 first_token_ms, submit_ms, reason_ms 를 찾는다.
    근거는 답과 같은 요청으로 내므로 reason_ms 는 submit_ms 와 같다. 값이 없는 칸(제출 안 함 등)은 넣지 않는다."""
    names = {"first_token_ms": "first_token", "submit_ms": "submitted", "reason_ms": "reason_sent"}
    return {app: int(timing[ours]) for app, ours in names.items() if isinstance(timing.get(ours), (int, float))}


def elapsed_ms(started):
    return round((time.perf_counter() - started) * 1000)


def solve(model, text, image, count, trace, *, choices=None, question=None, submit=None, started=None,
          gate_visible=False, eval_tag=None):
    """한 문제를 풀고(필요하면 제출까지) {"result", "notes", "timing", "seen", "think", "submission",
    "visible", "retake"} 을 돌려준다.

    submit(answer_part, reason) 은 제출 함수(없으면 제출하지 않는다). 객관식은 번호를 못 읽어도 반드시 답을 만든다.
    gate_visible=True(하드모드 사진, allow_guess 아님)면 VISIBLE: no 에서 멈추고 제출하지 않는다(result 는 retake).
    eval_tag 는 Langfuse metadata 의 eval 로 남긴다(측정 구분용). 단답을 끝내 못 만들면 QuizError(502).
    """
    started = started or time.perf_counter()
    timing, notes = {}, []
    system = IMAGE_SYSTEM if image is not None else TEXT_SYSTEM
    limit = QUIZ_MAX_TOKENS_IMAGE if image is not None else QUIZ_MAX_TOKENS_TEXT
    lines = QuizLines()
    error, retake, visible, missing = None, False, None, ""
    meta = {"mode": trace["mode"], "type": trace["type"], "image": image is not None,
            **({"eval": eval_tag} if eval_tag else {})}
    with tracing.observe("quiz-solve", "generation", model=model.model, input=trace, metadata=meta) as observation:
        stream = model.stream_quiz(system, text, image, limit, trace)
        try:
            for chunk in stream:
                timing.setdefault("first_token", elapsed_ms(started))
                lines.feed(chunk)
                if lines.visible_done and "visible" not in timing:
                    timing["visible"] = elapsed_ms(started)
                    visible, missing = parse_visible(lines.text("VISIBLE"))
                    if visible is False and gate_visible:
                        retake = True
                        break  # 답에 필요한 부분이 안 보인다 → 짐작하지 않고 멈춘다(제출하지 않음)
                if lines.answer_done and "answer" not in timing:
                    timing["answer"] = elapsed_ms(started)
                    if count and _choice(lines.text("ANSWER"), count, choices)[0] is None:
                        break  # 번호가 없으면 근거를 기다리지 않고 바로 다시 묻는다
                if lines.reason_done:
                    break  # 근거가 끝났다 → 더 읽지 않고 바로 제출
            else:
                lines.finish()
        except Exception as exc:  # 스트림 도중 실패: 읽은 데까지로 답을 만들어 본다
            error = exc
            lines.finish()
            log("WARNING", "퀴즈 스트림 실패", mode=trace["mode"], error=type(exc).__name__)
        finally:
            stream.close()
        if visible is None and lines.text("VISIBLE"):
            visible, missing = parse_visible(lines.text("VISIBLE"))
        observation.update(metadata={**meta, "visible": {True: "yes", False: "no"}.get(visible), "retake": retake})
    if retake:
        timing["total"] = elapsed_ms(started)
        log("INFO", "퀴즈 다시 찍기", mode=trace["mode"], missing=missing)
        return {"result": {"retake": True, "missing": missing or "답에 필요한 부분이 안 보임"}, "notes": ["retake"],
                "timing": timing, "seen": lines.text("SEEN"), "think": "", "submission": None,
                "answer_raw": "", "visible": "no", "retake": True}
    timing.setdefault("answer", elapsed_ms(started))
    answer, reason = lines.text("ANSWER"), quiz.clip_reason(lines.text("REASON"), QUIZ_REASON_MAX)
    if lines.reason_cut or len(lines.text("REASON")) > QUIZ_REASON_MAX:
        notes.append("reason_cut")  # 로그 fixed 로 근거가 잘렸는지 본다

    if count:
        number, fixed = _choice(answer, count, choices)
        if fixed:
            notes.append(fixed)
        if number is None:
            notes_part, number, reason = _retry_choice(model, text, image, count, trace, reason)
            notes += notes_part
        result = {"choice": number - 1, "reason": reason}
    else:
        if not answer:
            log("ERROR", "퀴즈 풀기 실패", mode=trace["mode"], error=type(error).__name__ if error else "NoAnswer")
            raise quiz.QuizError(502, "문제를 풀지 못했어요. 잠시 뒤 다시 보내 주세요.")
        # 하드모드는 문제 글이 없으므로, 모델이 SEEN 에 옮겨 적은 화면 글에서 '숫자만' 같은 형식 지시를 찾는다
        text_answer, short_notes = quiz.short_answer(answer, question or lines.text("SEEN"))
        result = {"text": text_answer, "reason": reason}
        notes += short_notes
    if visible is False:
        notes.append("guessed_not_visible")  # allow_guess 나 이지모드라 안 보여도 풀었다
    done = _finish(result, notes, timing, lines.text("SEEN"), lines.text("THINK"), submit, started)
    done["answer_raw"] = answer[:100]  # 로그용: 모델이 쓴 ANSWER 줄 그대로
    done["visible"] = {True: "yes", False: "no"}.get(visible)
    done["retake"] = False
    return done


# "2 (200)" 처럼 번호 뒤 괄호 안에 고른 보기 글. 보기 글 자체가 [..] 이어도 바깥 괄호 하나만 뗀다
_WITH_TEXT = re.compile(r"^(.*?)\s*[(（]\s*(.+?)\s*[)）]\s*$")


def _choice(answer, count, choices):
    """ANSWER 값에서 보기 번호(1부터)를 읽는다. (번호, 고친 내용 또는 None)

    "번호 (보기 글)" 이면 번호와 보기 글을 맞춰 보고, 이지모드(보기 목록이 있음)에서 둘이 다르면 보기 글을 믿는다.
    보기가 숫자인 문제(["10", "3", "7", "2"])에서 모델이 번호 대신 보기 글을 써도 바로잡기 위해서다.
    하드모드는 보기 목록이 없어 번호를 쓴다(보기 글은 로그에만 남는다).
    """
    match = _WITH_TEXT.match(answer or "")
    number_part, text_part = (match.group(1), match.group(2)) if match else (answer, None)
    by_text = quiz.match_choice(text_part, choices) if text_part and choices else None
    if number_part.strip():
        number = quiz.choice_number(number_part, count)
    else:  # "(4)" 처럼 괄호 안에 번호만 쓴 경우
        number = quiz.choice_number(text_part, count) if by_text is None else None
    if by_text is not None and by_text + 1 != number:
        return by_text + 1, "number_from_text"
    if number is None and choices and not match:
        found = quiz.match_choice(answer, choices)  # 번호 없이 보기 글만 쓴 경우
        return (found + 1, "number_from_text") if found is not None else (None, None)
    return number, None


def _retry_choice(model, text, image, count, trace, reason):
    """번호만 한 번 더 짧게 묻는다. 그래도 없으면 1번. (notes, 번호, 근거)"""
    log("WARNING", "퀴즈 보기 번호를 다시 물음", mode=trace["mode"])
    number = None
    try:
        with tracing.observe("quiz-retry-choice", "generation", model=model.model, input=trace,
                             metadata={"mode": trace["mode"], "type": trace["type"]}):
            again = model.complete_vision_json(quiz.RETRY_SYSTEM, text + "\n보기 번호만 답한다.", image, QUIZ_MAX_TOKENS,
                                               trace)
        number = quiz.choice_number(again.get("answer") if isinstance(again, dict) else None, count)
    except Exception as exc:
        log("WARNING", "퀴즈 보기 번호 다시 묻기 실패", error=type(exc).__name__)
    if number is not None:
        return ["retry_number"], number, reason or quiz.RETRY_REASON
    log("WARNING", "퀴즈 보기 번호를 못 찾아 1번을 냄", mode=trace["mode"])
    return ["fallback_first"], 1, quiz.FALLBACK_REASON


def _finish(result, notes, timing, seen, think, submit, started):
    result["reason"] = quiz.clip_reason(result["reason"], QUIZ_REASON_MAX)
    submission = None
    if submit is not None:
        answer_part = {k: result[k] for k in ("choice", "text") if k in result}
        submission = submit(answer_part, result["reason"])
        timing["submitted"] = timing["reason_sent"] = elapsed_ms(started)  # 근거도 같은 요청으로 보냈다
    timing["total"] = elapsed_ms(started)
    return {"result": result, "notes": notes, "timing": timing, "seen": seen, "think": think, "submission": submission}
