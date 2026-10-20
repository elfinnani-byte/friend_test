"""기억 3층: 최근 대화, 대화 요약, 기억 메모.

- 최근 대화: 메시지 원문. 답을 만들 때 최근 RECENT_TURNS 턴을 넣는다.
- 대화 요약: 요약되지 않은 메시지가 SUMMARIZE_AFTER 개를 넘으면 앞쪽 SUMMARIZE_BATCH 개를 요약에 합친다.
- 기억 메모: 매 턴 끝에 기억할 사실을 뽑아 추가, 수정, 삭제한다. 사용자가 화면에서 보고 지울 수 있다.
"""
import re

from app import when
from app.logs import log
from app.safety import is_sensitive

RECENT_TURNS = 10            # 답에 넣는 최근 대화(턴 = 사용자 + 캐릭터 메시지 한 쌍)
SUMMARIZE_AFTER = 40         # 요약되지 않은 메시지가 이 개수를 넘으면
SUMMARIZE_BATCH = 20         # 앞쪽 이만큼을 요약에 합친다
MAX_MEMORIES_IN_PROMPT = 30
KINDS = ("fact", "preference", "event")

EXTRACT_PROMPT = """너는 AI 친구 서비스의 기억 정리 담당이다. 방금 오간 대화에서 다음 대화에도 쓸 만한 사용자에 대한 사실을 정리한다.
- kind 는 fact(사실: 이름, 직업, 가족, 반려동물), preference(취향), event(날짜가 있는 일정이나 사건) 중 하나다.
- event 에는 대화에 나온 날짜를 [지금] 기준의 실제 날짜로 바꿔 함께 적는다. [달력]을 보고 요일을 맞춘다. 예: "10월 16일(금) 자격증 시험".
- 이미 있는 기억과 같은 내용이면 추가하지 않는다. 내용이 바뀌었으면 update 로 고친다. 사용자가 틀렸다거나 잊으라고 하면 remove 한다.
- 인사, 감탄, 그날의 기분처럼 다음 대화에 쓸모없는 것은 저장하지 않는다.
- 사용자가 나중에 알려 달라고 한 일은 약속 기능이 따로 저장하므로 기억에 넣지 않는다.
- 주민등록번호, 카드 번호, 계좌 번호, 비밀번호, 정확한 집 주소는 절대 저장하지 않는다.
- 저장할 것이 없으면 모두 빈 배열로 둔다.
반드시 JSON 하나로만 답한다: {"add": [{"kind": "fact", "text": "..."}], "update": [{"id": 0, "text": "..."}], "remove": [0]}"""

SUMMARY_PROMPT = """아래는 사용자와 AI 친구의 이전 대화다. 다음 대화에 이어 쓸 수 있도록 무슨 이야기를 했는지 요약한다.
기존 요약이 있으면 거기에 이어 붙여 하나의 요약으로 만든다. 10문장 이내로 쓴다."""

# fake 모드(모델 없음)에서 쓰는 간단한 기억 규칙
RULES = [
    (re.compile(r"내 이름은\s*([가-힣A-Za-z]{2,10}?)(?:이야|야|예요|이에요|입니다|이고|고)?(?=[\s.,!~]|$)"), "fact", "사용자 이름은 {0}"),
    (re.compile(r"([가-힣A-Za-z]{1,10}?)\s*(?:을|를)\s*(?:정말\s*|진짜\s*|엄청\s*)?좋아해"), "preference", "{0}{1} 좋아함"),
    (re.compile(r"(고양이|강아지|반려견|반려묘|동생|언니|오빠|형|누나)\s*이름은\s*([가-힣A-Za-z]{1,10}?)(?:이야|야|예요|이에요|입니다)?(?=[\s.,!~]|$)"), "fact", "{0} 이름은 {1}"),
    (re.compile(r"((?:오늘|내일|모레|[월화수목금토일]요일)[에는]*\s*[가-힣 ]{1,15}(?:시험|면접|발표|여행|약속))"), "event", "{0}"),
]


def object_particle(word):
    """받침이 있으면 '을', 없으면 '를'."""
    last = word[-1]
    if "가" <= last <= "힣":
        return "을" if (ord(last) - 0xAC00) % 28 else "를"
    return "을(를)"


def build_context(store, uid):
    """답을 만들 때 넣을 기억 메모, 대화 요약, 최근 대화를 꺼낸다."""
    memories = store.list_memories(uid)[-MAX_MEMORIES_IN_PROMPT:]
    summary = store.get_summary(uid)
    after = summary["covers_until"] if summary else 0
    recent = [m for m in store.recent_messages(uid, RECENT_TURNS * 2) if m["id"] > after]
    return memories, summary, recent


def memory_block(memories, summary):
    parts = []
    if memories:
        parts.append("[기억 메모: 사용자에 대해 알고 있는 것]\n" + "\n".join(f"- {m['text']}" for m in memories))
    if summary:
        parts.append(f"[이전 대화 요약]\n{summary['text']}")
    return "\n\n".join(parts)


def rule_extract(message):
    found = []
    for pattern, kind, template in RULES:
        for m in pattern.finditer(message):
            groups = [g.strip() for g in m.groups()]
            if "{1}" in template and len(groups) == 1:
                groups.append(object_particle(groups[0]))
            found.append({"kind": kind, "text": template.format(*groups)})
    return {"add": found, "update": [], "remove": []}


def update_memories(store, uid, user_message, reply, now, llm):
    """방금 턴에서 기억할 것을 뽑아 저장한다. 바뀐 내용을 돌려준다(화면 표시용)."""
    existing = store.list_memories(uid)
    if getattr(llm, "can_reason", False):
        listed = "\n".join(f"- id={m['id']} ({m['kind']}) {m['text']}" for m in existing) or "(없음)"
        user = f"[지금] {now:%Y-%m-%d %H:%M}\n[달력]\n{when.calendar_hint(now)}\n[이미 있는 기억]\n{listed}\n\n[대화]\nuser: {user_message}\nassistant: {reply}"
        try:
            plan = llm.complete_json(EXTRACT_PROMPT, user)
        except Exception as error:
            log("WARNING", "기억 정리 실패", error=type(error).__name__)
            return {"added": [], "updated": [], "removed": []}
    else:
        plan = rule_extract(user_message)

    known_ids = {m["id"]: m for m in existing}
    known_texts = {m["text"] for m in existing}
    changes = {"added": [], "updated": [], "removed": []}
    for item in plan.get("add") or []:
        text = str(item.get("text") or "").strip()[:200]
        kind = item.get("kind") if item.get("kind") in KINDS else "fact"
        if not text or text in known_texts or is_sensitive(text):
            continue
        changes["added"].append(store.add_memory(uid, kind, text))
    for item in plan.get("update") or []:
        text = str(item.get("text") or "").strip()[:200]
        if item.get("id") in known_ids and text and not is_sensitive(text):
            store.update_memory(uid, item["id"], text)
            changes["updated"].append({"id": item["id"], "text": text})
    for memory_id in plan.get("remove") or []:
        if memory_id in known_ids:
            store.delete_memory(uid, memory_id)
            changes["removed"].append({"id": memory_id, "text": known_ids[memory_id]["text"]})
    if any(changes.values()):
        log("INFO", "기억 정리", added=len(changes["added"]), updated=len(changes["updated"]), removed=len(changes["removed"]))
    return changes


def maybe_summarize(store, uid, llm):
    """요약되지 않은 메시지가 많으면 앞쪽을 요약에 합친다. 요약했으면 True."""
    summary = store.get_summary(uid)
    pending = store.messages_after(uid, summary["covers_until"] if summary else 0)
    if len(pending) <= SUMMARIZE_AFTER:
        return False
    batch = pending[:SUMMARIZE_BATCH]
    talk = "\n".join(f"{m['role']}: {m['content']}" for m in batch)
    previous = f"[기존 요약]\n{summary['text']}\n\n" if summary else ""
    if getattr(llm, "can_reason", False):
        text = llm.complete_text([{"role": "system", "content": SUMMARY_PROMPT},
                                  {"role": "user", "content": f"{previous}[대화]\n{talk}"}])
    else:
        text = ((summary["text"] + " ") if summary else "") + f"메시지 {len(batch)}개를 주고받았다."
    store.save_summary(uid, text[:2000], batch[-1]["id"])
    log("INFO", "대화 요약 갱신", covered=len(batch))
    return True
