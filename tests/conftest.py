"""테스트는 실제 AI를 부르지 않는다. 가짜 응답(fake)과 서버 메모리 저장소로 실행한다.

실제 AI 호출은 비용이 들고, 느리고, 같은 질문에도 답이 달라서 통과와 실패를 판정할 수 없다.
"""
import os

os.environ.update({"LLM_PROVIDER": "fake", "LLM_API_KEY": "", "STORAGE": "memory", "AUTH": "none"})
# 테스트는 Langfuse로 보내지 않는다. .env 에 키가 있어도 앱은 이미 있는 값을 덮어쓰지 않으므로 빈 값이 이긴다
os.environ.update({"LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""})
# 사진 글자 읽기(OCR)는 tesseract 프로그램이 필요하다. .env 에서 켜 두어도 테스트는 기본값(끔)으로 실행한다
os.environ["OCR_ENABLED"] = "false"
os.environ.setdefault("ENV", "test")
