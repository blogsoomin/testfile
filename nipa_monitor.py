"""
사업공고 모니터링 & 팀즈 알림 시스템
- 여러 사이트를 동시에 모니터링
- 매일 오전 9시 신규 공고 통합 알림
- 설정: config.json의 sites 배열에 사이트 추가
"""

import json
import hashlib
import logging
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

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

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE  = BASE_DIR / "seen_posts.json"

# ── 기본 설정 ──────────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    # [필수] Teams 워크플로 Webhook URL
    "teams_webhook_url": "https://YOUR_TENANT.webhook.office.com/...",

    # 전체 전송 모드 (GitHub Actions 수동 실행 시 true로 전환)
    "send_all": False,

    # 공통 요청 헤더
    "headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9",
    },
    "max_retries": 3,
    "retry_delay": 5,
    "timeout": 15,

    # ── 모니터링할 사이트 목록 ──────────────────────────────────────────────────
    # 사이트 추가 시 이 배열에 항목을 추가하세요.
    # selectors는 브라우저 F12 → 공고 제목 우클릭 → 검사로 확인
    "sites": [
        {
            "name": "NIPA",
            "url": "https://www.nipa.kr/biz/noticeList.do",
            "selectors": {
                "item":  "table tbody tr",
                "title": "td.tl a",
                "link_attr": "href",
                "date":  "span.bco",
            },
        },
        # 추가 예시 (실제 URL과 셀렉터로 교체하세요):
        # {
        #     "name": "IITP",
        #     "url": "https://www.iitp.kr/kr/1/business/businessList.it",
        #     "selectors": {
        #         "item":  "table tbody tr",
        #         "title": "td.subject a",
        #         "link_attr": "href",
        #         "date":  "td.date",
        #     },
        # },
    ],
}


# ── 설정 로드 ──────────────────────────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.warning("config.json을 기본값으로 생성했습니다. 설정 후 재실행하세요.")
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


# ── 상태 관리 ─────────────────────────────────────────────────────────────────
def load_state() -> dict:
    """seen_posts.json → {"사이트명": {"id1", "id2", ...}, ...}"""
    if STATE_FILE.exists():
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {k: set(v) for k, v in raw.get("sites", {}).items()}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(
            {"sites": {k: list(v) for k, v in state.items()},
             "updated": datetime.now().isoformat()},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )


def make_id(title: str, link: str) -> str:
    return hashlib.md5(f"{title}|{link}".encode()).hexdigest()


# ── 크롤링 ────────────────────────────────────────────────────────────────────
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
    notices = []
    for item in soup.select(selectors["item"]):
        title_el = item.select_one(selectors["title"])
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if not title:
            continue
        href = title_el.get(selectors.get("link_attr", "href"), "")
        if href and not href.startswith("http"):
            href = urljoin(base_url, href)
        date_el = item.select_one(selectors.get("date", ""))
        date = date_el.get_text(strip=True) if date_el else ""
        notices.append({"id": make_id(title, href), "title": title, "link": href, "date": date})
    return notices


# ── Teams 알림 ────────────────────────────────────────────────────────────────
def _is_power_automate_url(url: str) -> bool:
    return any(x in url for x in ["logic.azure.com", "powerautomate.com", "powerplatform.com"])


def matches_keywords(title: str, keywords: list[str]) -> bool:
    return any(kw.lower() in title.lower() for kw in keywords)


def send_teams_summary(webhook_url: str, results: list[dict], send_all: bool = False,
                       keywords: list[str] | None = None) -> bool:
    """
    results: [{"site": "NIPA", "notices": [...]}, ...]
    키워드 매칭 공고를 최상단에 표시
    """
    total = sum(len(r["notices"]) for r in results)
    if total == 0:
        log.info("전송할 공고 없음.")
        return True

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    prefix = "현재 공고 전체" if send_all else "신규 공고"
    keywords = keywords or []

    lines = [f"({total}건) {now}\n"]

    # ── 키워드 매칭 공고 최상단 표시 ─────────────────────────────────────────
    if keywords:
        matched = [
            (r["site"], n)
            for r in results
            for n in r["notices"]
            if matches_keywords(n["title"], keywords)
        ]
        if matched:
            kw_str = ", ".join(keywords)
            lines.append(f"🔥 관련 공고 ({kw_str}) — {len(matched)}건")
            for i, (site, n) in enumerate(matched, 1):
                date_str = n["date"] or "-"
                lines.append(f"  {i}. [{site}] [{date_str}] {n['title']}")
            lines.append("")

    # ── 사이트별 전체 목록 ────────────────────────────────────────────────────
    lines.append("📋 전체 목록")
    for r in results:
        if not r["notices"]:
            continue
        lines.append(f"▶ {r['site']} {len(r['notices'])}건")
        for i, n in enumerate(r["notices"], 1):
            date_str = n["date"] or "-"
            lines.append(f"  {i}. [{date_str}] {n['title']}")
        lines.append("")

    payload = {"text": "\n".join(lines).strip()}

    if not _is_power_automate_url(webhook_url):
        # 레거시 커넥터용 포맷
        payload = {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "text": f"📢 사업공고 {prefix} {total}건",
                         "weight": "Bolder", "size": "Large", "color": "Accent"},
                        {"type": "TextBlock", "text": "\n".join(lines).strip(), "wrap": True},
                    ],
                },
            }],
        }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info(f"Teams 알림 전송 완료 ({total}건)")
        return True
    except requests.RequestException as e:
        log.error(f"Teams 알림 전송 실패: {e}")
        return False


# ── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    log.info("=== 사업공고 모니터링 시작 ===")
    config = load_config()

    webhook_url = config["teams_webhook_url"]
    if "YOUR_TENANT" in webhook_url:
        log.error("config.json의 teams_webhook_url을 실제 URL로 교체하세요.")
        return

    send_all   = config.get("send_all", False)
    keywords   = config.get("keywords", [])
    state      = load_state()
    is_first   = len(state) == 0
    headers    = config["headers"]
    timeout    = config["timeout"]
    retries    = config["max_retries"]
    delay      = config["retry_delay"]

    results = []

    for site in config["sites"]:
        name = site["name"]
        log.info(f"[{name}] 크롤링 중...")

        soup = fetch_page(site["url"], headers, timeout, retries, delay)
        if soup is None:
            log.error(f"[{name}] 페이지 크롤링 실패")
            continue

        all_notices = parse_notices(soup, site["selectors"], site["url"])
        if not all_notices:
            log.warning(f"[{name}] 공고 파싱 실패 — selectors 확인 필요")
            continue

        log.info(f"[{name}] 파싱된 공고 수: {len(all_notices)}")
        site_seen = state.get(name, set())

        if send_all:
            target = all_notices
        elif is_first or not site_seen:
            # 첫 실행: 기준점만 저장
            state[name] = {n["id"] for n in all_notices}
            log.info(f"[{name}] 첫 실행 — 기준점 저장")
            continue
        else:
            target = [n for n in all_notices if n["id"] not in site_seen]

        if target:
            results.append({"site": name, "notices": target})
            state[name] = site_seen | {n["id"] for n in target}

    if is_first and not send_all:
        log.info("첫 실행 완료 — 다음 실행부터 신규 공고를 알립니다.")
        save_state(state)
        return

    save_state(state)

    if results:
        send_teams_summary(webhook_url, results, send_all=send_all, keywords=keywords)
    else:
        log.info("모든 사이트에 신규 공고 없음.")


if __name__ == "__main__":
    main()
