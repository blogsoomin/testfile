"""
NIPA 사업공고 모니터링 & 팀즈 알림 시스템
- 설정: config.json 파일을 수정하세요
- 실행: python nipa_monitor.py
- 자동화: cron 또는 Windows 작업 스케줄러로 주기적 실행
"""

import json
import os
import hashlib
import logging
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── 로깅 설정 ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("nipa_monitor.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── 경로 ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "seen_posts.json"

# ── 기본 설정 ──────────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    # [필수] Microsoft Teams Incoming Webhook URL
    # Teams 채널 → ... → 워크플로 → "팀즈에 게시" 또는 레거시 커넥터 URL 붙여넣기
    "teams_webhook_url": "https://YOUR_TENANT.webhook.office.com/webhookb2/...",

    # NIPA 사업공고 목록 페이지 URL
    # 실제 URL은 브라우저에서 사업공고 메뉴 클릭 후 주소창에서 확인하세요
    "notice_url": "https://www.nipa.kr/biz/noticeList.do",

    # 요청 헤더 (봇 차단 우회)
    "headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": "https://www.nipa.kr/"
    },

    # 페이지 파싱 CSS 셀렉터 (사이트 구조에 따라 조정 필요)
    # 브라우저 개발자 도구(F12)로 공고 목록 항목을 선택해 확인하세요
    "selectors": {
        # 공고 항목 하나씩을 감싸는 요소
        "item": "table tbody tr",
        # 공고 제목 링크 (td.tl 안의 a 태그)
        "title": "td.tl a",
        # 공고 링크 href
        "link_attr": "href",
        # 등록일 요소 (날짜 확인 후 업데이트 예정)
        "date": "span.bco",
    },

    # 요청 실패 시 재시도 횟수
    "max_retries": 3,
    # 재시도 대기 시간(초)
    "retry_delay": 5,
    # 타임아웃(초)
    "timeout": 15,
}


# ── 설정 로드 ──────────────────────────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.warning("config.json이 없어 기본값으로 생성했습니다. 설정을 수정 후 재실행하세요.")
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


# ── 상태 관리 (이미 본 공고 ID 저장) ──────────────────────────────────────────
def load_state() -> set:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("seen", []))
    return set()


def save_state(seen: set) -> None:
    STATE_FILE.write_text(
        json.dumps({"seen": list(seen), "updated": datetime.now().isoformat()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def make_id(title: str, link: str) -> str:
    """제목 + 링크로 고유 ID 생성"""
    raw = f"{title}|{link}"
    return hashlib.md5(raw.encode()).hexdigest()


# ── 페이지 크롤링 ──────────────────────────────────────────────────────────────
def fetch_page(url: str, headers: dict, timeout: int, retries: int, delay: int) -> BeautifulSoup | None:
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as e:
            log.warning(f"요청 실패 ({attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(delay)
    return None


def parse_notices(soup: BeautifulSoup, selectors: dict, base_url: str) -> list[dict]:
    """공고 목록 파싱 → [{"id", "title", "link", "date"}, ...]"""
    notices = []
    items = soup.select(selectors["item"])

    for item in items:
        title_el = item.select_one(selectors["title"])
        if not title_el:
            continue

        title = title_el.get_text(strip=True)
        if not title:
            continue

        href = title_el.get(selectors.get("link_attr", "href"), "")
        # 상대 경로 처리
        if href and not href.startswith("http"):
            from urllib.parse import urljoin
            href = urljoin(base_url, href)

        date_el = item.select_one(selectors.get("date", "td.date"))
        date = date_el.get_text(strip=True) if date_el else ""

        notices.append({
            "id": make_id(title, href),
            "title": title,
            "link": href,
            "date": date,
        })

    return notices


# ── Teams 알림 ─────────────────────────────────────────────────────────────────
def _is_power_automate_url(url: str) -> bool:
    """워크플로(Power Automate) URL인지 판별"""
    return any(x in url for x in ["logic.azure.com", "powerautomate.com", "powerplatform.com"])


def send_teams_notification(webhook_url: str, notices: list[dict], title_prefix: str = "신규") -> bool:
    """새 공고를 Teams에 전송 — 워크플로/레거시 커넥터 자동 구분"""
    if not notices:
        return True

    if _is_power_automate_url(webhook_url):
        return _send_via_power_automate(webhook_url, notices, title_prefix)
    else:
        return _send_via_legacy_connector(webhook_url, notices, title_prefix)


def _send_via_power_automate(webhook_url: str, notices: list[dict], title_prefix: str = "신규") -> bool:
    """Power Automate 워크플로 Webhook (최신 Teams)"""
    lines = []
    for n in notices:
        date_str = f"[{n['date']}] " if n["date"] else ""
        if n["link"]:
            lines.append(f"• {date_str}<a href='{n['link']}'>{n['title']}</a>")
        else:
            lines.append(f"• {date_str}{n['title']}")

    body = (
        f"<h3>📢 NIPA 사업공고 {title_prefix} {len(notices)}건</h3>"
        f"<p>{datetime.now().strftime('%Y-%m-%d %H:%M')}</p>"
        + "<br>".join(lines)
        + f"<br><br><a href='https://www.nipa.kr/'>NIPA 사업공고 바로가기 →</a>"
    )

    payload = {"text": body}

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info(f"Teams 알림 전송 완료 (Power Automate, {len(notices)}건)")
        return True
    except requests.RequestException as e:
        log.error(f"Teams 알림 전송 실패: {e}")
        return False


def _send_via_legacy_connector(webhook_url: str, notices: list[dict], title_prefix: str = "신규") -> bool:
    """레거시 Incoming Webhook 커넥터 (구버전 Teams)"""
    facts = [
        {
            "type": "FactSet",
            "facts": [
                {"title": f"[{n['date']}]" if n["date"] else "📌",
                 "value": f"[{n['title']}]({n['link']})" if n["link"] else n["title"]}
                for n in notices
            ],
        }
    ]

    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": f"📢 NIPA 사업공고 {title_prefix} {len(notices)}건",
                            "weight": "Bolder",
                            "size": "Large",
                            "color": "Accent",
                        },
                        {
                            "type": "TextBlock",
                            "text": datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "isSubtle": True,
                            "size": "Small",
                        },
                        *facts,
                        {
                            "type": "ActionSet",
                            "actions": [
                                {
                                    "type": "Action.OpenUrl",
                                    "title": "NIPA 사업공고 바로가기",
                                    "url": "https://www.nipa.kr/",
                                }
                            ],
                        },
                    ],
                },
            }
        ],
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info(f"Teams 알림 전송 완료 (레거시 커넥터, {len(notices)}건)")
        return True
    except requests.RequestException as e:
        log.error(f"Teams 알림 전송 실패: {e}")
        return False


# ── 메인 ───────────────────────────────────────────────────────────────────────
def main():
    log.info("=== NIPA 사업공고 모니터링 시작 ===")
    config = load_config()

    webhook_url = config["teams_webhook_url"]
    if "YOUR_TENANT" in webhook_url:
        log.error("config.json의 teams_webhook_url을 실제 Webhook URL로 교체하세요.")
        return

    seen = load_state()
    is_first_run = len(seen) == 0

    soup = fetch_page(
        url=config["notice_url"],
        headers=config["headers"],
        timeout=config["timeout"],
        retries=config["max_retries"],
        delay=config["retry_delay"],
    )

    if soup is None:
        log.error("페이지 크롤링 실패. notice_url 또는 네트워크를 확인하세요.")
        return

    notices = parse_notices(soup, config["selectors"], config["notice_url"])

    if not notices:
        log.warning(
            "공고를 파싱하지 못했습니다. config.json의 selectors를 브라우저 F12로 확인해 수정하세요."
        )
        return

    log.info(f"파싱된 공고 수: {len(notices)}")

    # 전체 전송 모드 (수동 실행 시 선택 가능)
    if config.get("send_all"):
        log.info("전체 전송 모드 — 현재 목록 전체를 Teams로 전송합니다.")
        send_teams_notification(webhook_url, notices, title_prefix="현재 공고 전체")
        save_state({n["id"] for n in notices})
        return

    if is_first_run:
        log.info("첫 실행 — 현재 공고를 기준점으로 저장합니다. 다음 실행부터 새 공고를 알립니다.")
        save_state({n["id"] for n in notices})
        return

    new_notices = [n for n in notices if n["id"] not in seen]

    if new_notices:
        log.info(f"신규 공고 {len(new_notices)}건 발견!")
        send_teams_notification(webhook_url, new_notices)
        seen.update(n["id"] for n in new_notices)
        save_state(seen)
    else:
        log.info("신규 공고 없음.")


if __name__ == "__main__":
    main()
