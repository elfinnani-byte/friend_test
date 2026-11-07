# 퀴즈 시험지 (틀린 문제 모음)

대회·연습에서 틀린 문제를 모아 두고, `scripts/quiz_eval.py` 로 실제 모델이 다시 풀게 해 정답률과 시간을 봅니다.
테스트(`pytest`)는 이 폴더의 문제로 모델을 부르지 않습니다. 스크립트를 직접 실행할 때만 LLM 을 부릅니다.

## 넣는 방법

1. 그림이 있는 문제는 이 폴더에 그림 파일을 넣습니다(PNG, JPEG, WebP, GIF, 5MB 이하).
   - 하드모드: 퀴즈 화면을 폰으로 찍은 사진
   - 이지모드 그림 문제: 퀴즈 서버가 준 그림
2. `questions.json` 에 문제를 한 줄씩(목록의 한 항목씩) 적습니다. 처음이면 `questions.example.json` 을 복사해 시작하세요.

## `questions.json` 형식

목록 하나이고, 항목마다 아래 칸을 씁니다.

| 칸 | 필수 | 뜻 |
| --- | --- | --- |
| `id` | 예 | 문제 이름(겹치지 않게). 표에 이 이름으로 나옵니다 |
| `mode` | 예 | `"hard"`(사진만) 또는 `"easy"`(문제 글이 있음) |
| `type` | 예 | `"choice"`(객관식) 또는 `"short"`(단답) |
| `image` | 하드모드는 예 | 이 폴더 안 그림 파일 이름. 이지모드 그림 문제면 함께 적습니다 |
| `question` | 이지모드는 예 | 문제 글 |
| `choices` | 이지모드 객관식은 예 | 보기 글 목록 |
| `choice_count` | 하드모드 객관식은 예 | 보기 개수 |
| `answer` | 예 | 정답. 객관식은 **0부터 센 보기 번호**(화면의 1번 = `0`), 단답은 글(`"8"`) |
| `answers` | 아니요 | 단답에서 정답으로 칠 다른 표기 목록(`["8", "여덟"]`). 띄어쓰기·대소문자·문장 부호는 빼고 비교합니다 |
| `note` | 아니요 | 왜 틀렸는지 메모(표에는 나오지 않습니다) |

예:

```json
[
  {"id": "hard-capital", "mode": "hard", "type": "choice", "image": "capital.png", "choice_count": 4, "answer": 2},
  {"id": "hard-code", "mode": "hard", "type": "short", "image": "code.png", "answer": "8"},
  {"id": "easy-http", "mode": "easy", "type": "choice", "question": "성공을 뜻하는 HTTP 상태 코드는?",
   "choices": ["404", "200", "500", "301"], "answer": 1},
  {"id": "easy-picture", "mode": "easy", "type": "short", "question": "그림 속 빨간 동그라미는 몇 개? (숫자만)",
   "image": "shapes.png", "answer": "5"}
]
```

## 실행

```bash
# .env 의 키와 QUIZ_MODEL, QUIZ_MODEL_TEXT 로 실제 모델을 부릅니다(요금이 듭니다)
python scripts/quiz_eval.py
python scripts/quiz_eval.py --repeat 3            # 문제마다 3번씩(흔들림 보기)
python scripts/quiz_eval.py --only hard-code      # 한 문제만
python scripts/quiz_eval.py --image-model claude-sonnet-5-5 --text-model claude-haiku-5-5
```

결과는 문제마다 정답·모델 답·맞음·시간(첫 토큰, 답 확정, 전체)과, 마지막에 정답률과 평균 시간을 보여 줍니다.
퀴즈 서버에는 제출하지 않습니다.
