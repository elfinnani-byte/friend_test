"""코드의 프롬프트를 Langfuse 프롬프트 관리에 처음 올린다.

  app/router.py 의 ROUTER_TEMPLATE → router (text, 라벨 production, staging)

실행:
  python scripts/upload_prompts.py

- 키는 .env 의 LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL 을 쓴다. 값은 출력하지 않는다.
- 같은 이름의 프롬프트가 이미 있으면 만들지 않는다. 같은 이름으로 다시 만들면 새 버전이 생기기 때문이다.
  문장을 고친 새 버전은 Langfuse 화면에서 만들고, 라벨을 옮겨 배포한다.
- 끝나면 프롬프트 이름, 버전, 라벨만 출력한다.

근거(Python SDK v5):
  https://langfuse.com/docs/prompt-management/get-started
  create_prompt — "If you already have a prompt with the same name, the prompt will be added as a new version."
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_dotenv  # noqa: E402
from app.router import PROMPT_NAME, ROUTER_TEMPLATE  # noqa: E402

PROMPTS = [
    {"name": PROMPT_NAME, "prompt": ROUTER_TEMPLATE, "labels": ["production", "staging"],
     "commit_message": "app/router.py 의 router_prompt 문장을 옮김"},
]


def existing(langfuse, name):
    """이름이 같은 프롬프트의 정보(버전, 라벨). 없으면 None.
    조회가 실패하면 '없음'으로 보지 않고 멈춘다. 있는데 못 본 채 만들면 새 버전이 생기기 때문이다."""
    found = [p for p in langfuse.api.prompts.list(name=name).data if p.name == name]
    return found[0] if found else None


def main():
    load_dotenv(ROOT / ".env")
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        sys.exit("LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY 가 없어 올리지 않습니다.")

    from langfuse import Langfuse  # .env 를 읽은 뒤에 만든다

    langfuse = Langfuse(tracing_enabled=False)
    for spec in PROMPTS:
        found = existing(langfuse, spec["name"])
        if found is not None:
            print(f"{spec['name']}: 이미 있어 만들지 않음 (버전 {found.versions}, 라벨 {found.labels})")
            continue
        created = langfuse.create_prompt(type="text", **spec)
        print(f"{created.name}: v{created.version} 만듦 (라벨 {created.labels})")


if __name__ == "__main__":
    main()
