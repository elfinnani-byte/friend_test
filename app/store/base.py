"""저장소 고르기. STORAGE 환경변수에 따라 메모리 저장소나 Supabase 저장소를 만든다."""

DEFAULT_SETTINGS = {"location": "서울", "timezone": "Asia/Seoul", "push_enabled": False}


def make_store(settings):
    if settings.storage == "supabase":
        from app.store.supabase_store import SupabaseStore
        return SupabaseStore(settings.supabase_url, settings.supabase_secret_key)
    from app.store.memory_store import MemoryStore
    return MemoryStore()
