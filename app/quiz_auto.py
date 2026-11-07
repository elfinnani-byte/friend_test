"""이지모드 서버 직접 풀기(/api/quiz/auto/*).

start 를 받으면 퀴즈 서버의 /api/question 을 0.5초마다 확인하다가, 글이 있는 이지모드 문제가 열리면 바로 풀어 제출한다.
- 하드모드(hidden) 문제는 건너뛰고 앱이 풀게 둔다. 이미 답을 낸 문제(my_answer)도 건너뛴다.
- finished 가 되거나, stop 을 받거나, 30분이 지나거나, 퀴즈 서버가 401(초기화)을 주면 멈춘다.
- 같은 token 으로는 하나만 돈다. 진행 상태는 이 인스턴스의 메모리에만 있다.
- 팀 token 은 로그와 상태 응답에 넣지 않는다. 안에서는 token 의 해시로 구분한다.
"""
import hashlib
import threading
import time
from collections import deque

import httpx

from app import quiz
from app.logs import log

MAX_RECENT = 10


def token_key(token):
    return hashlib.sha256(token.encode()).hexdigest()[:16]


class AutoRun:
    """token 하나의 직접 풀기. run() 이 끝날 때까지 퀴즈 서버를 확인한다."""

    def __init__(self, base, token, solve_question, http, *, poll=0.5, max_seconds=1800,
                 clock=time.monotonic, sleep=time.sleep):
        self.base, self.token = base, token
        self.solve_question, self.http = solve_question, http
        self.poll, self.clock, self.sleep = poll, clock, sleep
        self.started_at = clock()
        self.deadline = self.started_at + max_seconds
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.handled = set()              # 풀었거나 건너뛴 문제 id
        self.recent = deque(maxlen=MAX_RECENT)
        self.state, self.index, self.total, self.current = None, None, None, None
        self.stop_reason = None

    # ── 상태 ──
    def running(self):
        return self.stop_reason is None

    def snapshot(self):
        with self.lock:
            return {"running": self.running(), "stop_reason": self.stop_reason, "state": self.state,
                    "index": self.index, "total": self.total,
                    "question_number": self.index + 1 if isinstance(self.index, int) else None,
                    "solving": self.current, "results": list(self.recent),
                    "ends_in_sec": max(0, round(self.deadline - self.clock())) if self.running() else 0}

    def _record(self, item):
        with self.lock:
            self.recent.appendleft(item)

    def _stop(self, reason):
        if self.stop_reason is None:
            self.stop_reason = reason
            log("INFO", "퀴즈 직접 풀기 멈춤", host=quiz_host(self.base), reason=reason, solved=len(self.recent))

    # ── 실행 ──
    def run(self):
        log("INFO", "퀴즈 직접 풀기 시작", host=quiz_host(self.base))
        failures = 0
        while self.running():
            if self.stop_event.is_set():
                self._stop("stopped")
                break
            if self.clock() >= self.deadline:
                self._stop("timeout")
                break
            try:
                r = self.http.get(f"{self.base}/api/question", headers={"Authorization": f"Bearer {self.token}"})
            except httpx.HTTPError as error:
                failures += 1
                log("WARNING", "퀴즈 직접 풀기 문제 확인 실패", error=type(error).__name__, failures=failures)
                self.sleep(min(5, self.poll * (1 + failures)))
                continue
            if r.status_code == 401:
                self._stop("unauthorized")  # 퀴즈 서버 초기화. 앱이 다시 참가한 뒤 start 를 다시 보낸다
                break
            if not r.is_success:
                failures += 1
                self.sleep(min(5, self.poll * (1 + failures)))
                continue
            failures = 0
            self.step(r.json())
            if self.running():
                self.sleep(self.poll)

    def step(self, data):
        """문제 상태 한 번을 처리한다."""
        with self.lock:
            self.state, self.index, self.total = data.get("state"), data.get("index"), data.get("total")
        if self.state == "finished":
            self._stop("finished")
            return
        question = data.get("question") or {}
        qid = question.get("id")
        if self.state != "open" or not qid or qid in self.handled:
            return
        self.handled.add(qid)
        if data.get("my_answer") is not None:
            self._record({"question_id": qid, "index": self.index, "skipped": "already_answered"})
            return
        if question.get("hidden") or question.get("hard") or not question.get("text"):
            # 하드모드: 문제 글이 없으므로 앱이 사진으로 푼다
            self._record({"question_id": qid, "index": self.index, "skipped": "hidden"})
            return
        with self.lock:
            self.current = qid
        try:
            item = self.solve_question(self.base, self.token, question)
        except Exception as error:
            item = {"error": type(error).__name__}
            log("WARNING", "퀴즈 직접 풀기 실패", error=type(error).__name__)
        finally:
            with self.lock:
                self.current = None
        self._record({"question_id": qid, "index": self.index, **item})


class AutoManager:
    """token 마다 AutoRun 하나. 같은 token 으로 start 를 다시 받으면 새로 만들지 않는다."""

    def __init__(self, solve_question=None, http=None, **run_options):
        self.solve_question = solve_question
        self.http = http or httpx.Client(timeout=5.0, follow_redirects=False)
        self.run_options = run_options
        self.runs = {}
        self.lock = threading.Lock()

    def start(self, base, token, solve_question=None, background=True):
        """(새로 시작했는지, 상태). solve_question 을 주면 그것으로 푼다(요청마다 고른 모델).
        background=False 면 지금 스레드에서 끝까지 돈다(테스트용)."""
        key = token_key(token)
        with self.lock:
            run = self.runs.get(key)
            if run is not None and run.running():
                return False, run.snapshot()
            run = AutoRun(base, token, solve_question or self.solve_question, self.http, **self.run_options)
            self.runs[key] = run
        if background:
            threading.Thread(target=run.run, name=f"quiz-auto-{key}", daemon=True).start()
        else:
            run.run()
        return True, run.snapshot()

    def stop(self, token):
        run = self.runs.get(token_key(token))
        if run is None or not run.running():
            return False
        run.stop_event.set()
        return True

    def status(self, token):
        run = self.runs.get(token_key(token))
        return run.snapshot() if run is not None else None


def quiz_host(base):
    from urllib.parse import urlsplit
    return urlsplit(base).hostname
