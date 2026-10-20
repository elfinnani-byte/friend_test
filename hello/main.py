"""배포 연습용 최소 앱. 화면 한 장과 /health 만 있고 AI를 부르지 않는다."""
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()


@app.get("/", response_class=HTMLResponse)
def home():
    return "<h1>안녕하세요, hello 앱입니다</h1>"


@app.get("/health")
def health():
    return {"status": "ok"}
