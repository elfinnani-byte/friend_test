"""data/eval/ 의 평가 데이터를 Langfuse 데이터셋으로 올린다.

  tool_selection.jsonl     → tool-selection
  memory_extraction.jsonl  → memory-extraction
  safety.jsonl             → safety
  holiday_reply.jsonl      → holiday-reply

실행:
  python scripts/upload_datasets.py

- 키는 .env 의 LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL 을 쓴다. 값은 출력하지 않는다.
- 항목 id 는 '<데이터셋 이름>-<파일의 id>' 로 고정한다. 같은 id 로 다시 올리면 새로 만들지 않고 고친다(upsert).
  그래서 여러 번 실행해도 항목 수가 늘지 않는다.
- 끝나면 데이터셋별 항목 수만 출력한다.

근거(Python SDK v5):
  https://langfuse.com/docs/evaluation/experiments/datasets
  create_dataset_item(id=...) — "Upserts if an item with id already exists."
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_dotenv  # noqa: E402

EVAL = ROOT / "data" / "eval"
TOOL_EVAL_TIME = "2026-10-12"  # tool_selection 정답을 만든 '지금'(scripts/eval_tools.py 와 같다)
HOLIDAY_NOW = "2026-11-02(월) 14:00"  # holiday_reply 의 '지금'
NEXT_HOLIDAY = {"T26": "2026-12-25"}  # 다음 공휴일을 묻는 항목. TOOL_EVAL_TIME 기준 정답


def read_jsonl(name):
    with open(EVAL / name, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def tool_selection_items():
    for row in read_jsonl("tool_selection.jsonl"):
        expected = {"tools": row["expected_tools"], "optional_tools": row["optional_tools"], "args": row["expected_args"]}
        metadata = {"id": row["id"], "category": "+".join(row["expected_tools"]) or "none"}  # 정답 도구 조합
        if row.get("note"):
            metadata["note"] = row["note"]
        if row["id"] in NEXT_HOLIDAY:
            expected["next_holiday"] = NEXT_HOLIDAY[row["id"]]
            metadata["evaluated_at"] = TOOL_EVAL_TIME
        yield row["id"], {"message": row["message"], "history": row["history"]}, expected, metadata


def memory_extraction_items():
    for row in read_jsonl("memory_extraction.jsonl"):
        category = "remember" if row["must_include"] else "ignore"  # 기억할 말이 있는지
        yield (row["id"], {"conversation": row["conversation"]},
               {"must_include": row["must_include"], "must_not_include": row["must_not_include"]},
               {"id": row["id"], "category": category})


def safety_items():
    for row in read_jsonl("safety.jsonl"):
        yield row["id"], {"message": row["message"]}, row["expected"], {"id": row["id"], "category": row["category"]}


def holiday_reply_items():
    for row in read_jsonl("holiday_reply.jsonl"):
        metadata = {"id": row["id"], "kind": row["kind"], "now": HOLIDAY_NOW}
        if row["kind"] == "boundary":
            metadata["note"] = "도구 결과는 시험용 가정"
        yield row["id"], {"message": row["message"], "tool_result": row["tool_result"]}, row["expected"], metadata


DATASETS = {
    "tool-selection": tool_selection_items,
    "memory-extraction": memory_extraction_items,
    "safety": safety_items,
    "holiday-reply": holiday_reply_items,
}


def ensure_dataset(langfuse, name):
    """이름이 같은 데이터셋이 있으면 그대로 쓰고, 없을 때만 만든다."""
    try:
        langfuse.get_dataset(name)
    except Exception:
        langfuse.create_dataset(name=name)


def main():
    load_dotenv(ROOT / ".env")
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        sys.exit("LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY 가 없어 올리지 않습니다.")

    from langfuse import Langfuse  # .env 를 읽은 뒤에 만든다

    langfuse = Langfuse()
    for name, items in DATASETS.items():
        ensure_dataset(langfuse, name)
        for item_id, input_, expected, metadata in items():
            langfuse.create_dataset_item(dataset_name=name, id=f"{name}-{item_id}", input=input_,
                                         expected_output=expected, metadata=metadata)
    langfuse.flush()
    for name in DATASETS:
        print(f"{name}: {len(langfuse.get_dataset(name).items)}개")


if __name__ == "__main__":
    main()
