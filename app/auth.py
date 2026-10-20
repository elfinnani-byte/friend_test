"""요청을 보낸 사용자 확인하기.

- AUTH=none: 로그인 없이 모든 요청을 개발용 사용자 한 명으로 본다. 노트북과 첫 배포용이다.
- AUTH=supabase: 화면이 Supabase Auth로 로그인(익명 또는 구글)하고 받은 access token을 요청마다 보낸다.
  서버는 그 토큰을 Supabase Auth에 물어 확인하고 user_id 를 얻는다. 확인한 user_id 만 저장소 조회에 쓴다.
"""
import time
from dataclasses import dataclass

import httpx
from fastapi import HTTPException, Request

from app.logs import log

LOCAL_USER = "local-user"
CACHE_SECONDS = 300


@dataclass
class User:
    id: str
    anonymous: bool
    email: str | None = None


class Auth:
    def __init__(self, settings):
        self.mode = settings.auth
        self.url = settings.supabase_url
        self.key = settings.supabase_publishable_key
        self.cache = {}  # token → (만료 시각, User)

    def __call__(self, request: Request) -> User:
        if self.mode == "none":
            return User(LOCAL_USER, anonymous=True)
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise HTTPException(401, "로그인이 필요합니다")
        return self.verify(header[7:].strip())

    def verify(self, token):
        hit = self.cache.get(token)
        if hit and hit[0] > time.time():
            return hit[1]
        try:
            r = httpx.get(f"{self.url}/auth/v1/user", timeout=5,
                          headers={"apikey": self.key, "Authorization": f"Bearer {token}"})
        except httpx.HTTPError as error:
            log("ERROR", "로그인 확인 요청 실패", error=type(error).__name__)
            raise HTTPException(503, "로그인을 확인하지 못했습니다") from error
        if r.status_code != 200:
            raise HTTPException(401, "로그인이 만료되었거나 올바르지 않습니다")
        data = r.json()
        user = User(data["id"], anonymous=bool(data.get("is_anonymous")), email=data.get("email") or None)
        if len(self.cache) > 1000:
            self.cache.clear()
        self.cache[token] = (time.time() + CACHE_SECONDS, user)
        return user
