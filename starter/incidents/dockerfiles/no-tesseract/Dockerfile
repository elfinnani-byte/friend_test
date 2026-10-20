# 에이전트 서비스 실행 환경. 이 파일을 저장소 루트로 복사해 쓴다(Dockerfile).
# 1) 시작 이미지와 Python 버전을 고정한다.
FROM python:3.13-slim-bookworm

WORKDIR /app

# 3) 라이브러리 설치. requirements.txt 만 먼저 복사하면 코드만 바뀐 빌드에서 이 단계를 다시 쓰지 않는다.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4) 코드 복사. 올리지 않을 파일은 .dockerignore 에 적는다(.env 포함).
COPY . .

# 5) 관리자(root)가 아닌 사용자로 실행한다.
RUN useradd --create-home appuser
USER appuser

# 6) Cloud Run이 정해 주는 PORT 환경변수로 실행한다. 노트북에서는 기본값 8080.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
