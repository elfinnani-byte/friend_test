"""배포하는 동안 서비스가 끊기는지 세어 보는 스크립트.

서비스 주소의 /version 에 일정한 간격으로 요청을 보내고, 응답 코드와 응답한 리비전을 기록한다.
AI를 부르지 않는 /version 만 요청하므로 AI 비용이 들지 않는다. 최대 시간이 지나면 스스로 멈춘다.

실행:
  python scripts/watch_requests.py https://<서비스 주소>              # 0.2초 간격, 최대 5분
  python scripts/watch_requests.py https://<서비스 주소> --seconds 120
멈추려면 Ctrl+C. 멈추면 요약을 보여 준다.
"""
import argparse
import time
from collections import Counter
from datetime import datetime

import httpx

MAX_SECONDS = 300


def main():
    parser = argparse.ArgumentParser(description="배포 중 요청 성공·실패 세기")
    parser.add_argument("url", help="서비스 주소 (예: https://friend-agent-staging-xxxx.run.app)")
    parser.add_argument("--interval", type=float, default=0.2, help="요청 간격(초), 기본 0.2")
    parser.add_argument("--seconds", type=int, default=MAX_SECONDS, help=f"최대 실행 시간(초), 기본 {MAX_SECONDS}, 상한 {MAX_SECONDS}")
    args = parser.parse_args()
    url = args.url.rstrip("/") + "/version"
    limit = min(args.seconds, MAX_SECONDS)
    interval = max(args.interval, 0.1)

    total, failures, by_revision, fail_times = 0, 0, Counter(), []
    last_revision = None
    started = time.time()
    print(f"{url} 에 {interval}초마다 요청합니다. 최대 {limit}초 뒤 멈춥니다. 멈추려면 Ctrl+C.")
    try:
        with httpx.Client(timeout=2.0) as client:
            while time.time() - started < limit:
                now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                total += 1
                try:
                    r = client.get(url)
                    if r.status_code == 200:
                        revision = r.json().get("revision", "?")
                        by_revision[revision] += 1
                        if revision != last_revision:
                            print(f"{now}  응답 리비전: {revision}")
                            last_revision = revision
                    else:
                        failures += 1
                        fail_times.append(now)
                        print(f"{now}  실패: 응답 코드 {r.status_code}")
                except httpx.HTTPError as error:
                    failures += 1
                    fail_times.append(now)
                    print(f"{now}  실패: {type(error).__name__}")
                time.sleep(interval)
    except KeyboardInterrupt:
        pass

    print("\n── 요약 ──")
    print(f"전체 요청 {total}건, 실패 {failures}건, 걸린 시간 {time.time() - started:.0f}초")
    for revision, count in by_revision.most_common():
        print(f"  {revision}: {count}건 ({count / max(total, 1):.0%})")
    if fail_times:
        print(f"실패 시각: 처음 {fail_times[0]}, 마지막 {fail_times[-1]}")
    print("실패가 있으면 그 시각을 배포 단계(콘솔, Actions 로그의 시각)와 맞춰 본다. 내 네트워크 문제도 실패로 잡힐 수 있다.")


if __name__ == "__main__":
    main()
