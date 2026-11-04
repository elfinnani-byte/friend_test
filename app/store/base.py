"""저장소 고르기. STORAGE 환경변수에 따라 메모리 저장소나 Supabase 저장소를 만든다."""

class DuplicateReference(Exception):
    """크레딧 원장에 이미 있는 참조(ref)로 기록하려 했다. 데이터베이스의 유일 제약 위반과 같은 뜻이다."""


DEFAULT_SETTINGS = {"location": "서울", "timezone": "Asia/Seoul", "push_enabled": False}


def make_store(settings):
    if settings.storage == "supabase":
        from app.store.supabase_store import SupabaseStore
        return SupabaseStore(settings.supabase_url, settings.supabase_secret_key)
    from app.store.memory_store import MemoryStore
    return MemoryStore()
