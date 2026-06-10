#!/usr/bin/env python3
"""
Woomi 증권 종합 잔고 — 현재가 자동 업데이트 (v3)
GitHub Actions에서 5분마다 실행됩니다 (cron-job.org → repository_dispatch).

v3 변경사항 (v2 대비):
3. 국내주식 NXT(넥스트레이드) 프리/애프터마켓 가격 반영
   - 네이버페이 증권 모바일 API에서 시세 조회 (NXT 프리 08:00~08:50 /
     메인 09:00~15:20 / 애프터 15:30~20:00 가격이 실시간 반영됨)
   - NXT 운영시간(프리/애프터)에는 NXT 가격, KRX 정규장에는 KRX 가격 사용
   - 네이버 API 실패 시 기존 yfinance(.KS/.KQ 폴백)로 자동 전환
   - 네이버 응답에서 NXT 가격 필드를 자동 탐색하므로 필드명이 바뀌어도 대응
   - 보너스: 네이버는 6자리 코드만으로 조회되므로 .KS/.KQ 구분이 불필요

v2 기능 유지:
1. 티커 하드코딩 없음 — 노션 '티커' 값으로 자동 판별
2. 미국주식 프리/애프터마켓 가격 반영
"""

import re
import os
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

# ─────────────────────────────────────────────
# 환경변수 (GitHub Secrets에서 주입)
# ─────────────────────────────────────────────
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_ID = os.environ.get("NOTION_DB_ID", "2b7bf6e4578e802683b8f3e28bc9f61b")
DEBUG_NAVER = os.environ.get("DEBUG_NAVER", "") == "1"   # 1이면 네이버 응답 구조 로그 출력

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

# 네이버 API 호출용 (브라우저 흉내 — 차단 방지)
NAVER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Referer": "https://m.stock.naver.com/",
}

KST = ZoneInfo("Asia/Seoul")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

RE_KRX = re.compile(r"^\d[0-9A-Z]{5}$")           # 035900, 0052S0 등 6자리 국내코드
RE_US = re.compile(r"^[A-Z]{1,5}([.\-][A-Z])?$")  # TSLA, BRK.B 등 미국 티커

HAS_MARKET_STATE_COL = False
HAS_SYMBOL_COL = False


# ─────────────────────────────────────────────
# USD/KRW 환율 (1회 조회 후 재사용)
# ─────────────────────────────────────────────
_usd_krw = None


def get_usd_krw() -> float:
    global _usd_krw
    if _usd_krw:
        return _usd_krw
    try:
        rate = yf.Ticker("USDKRW=X").fast_info.last_price
        _usd_krw = float(rate)
        log.info(f"USD/KRW = {_usd_krw:,.2f}")
        return _usd_krw
    except Exception as e:
        log.warning(f"환율 조회 실패, 기본값 1479 사용: {e}")
        _usd_krw = 1479.0
        return _usd_krw


# ─────────────────────────────────────────────
# 티커 자동 판별 (v2와 동일)
# ─────────────────────────────────────────────
def resolve_ticker(ticker: str, symbol_override: str = ""):
    t = ticker.upper().strip()

    if symbol_override:
        s = symbol_override.strip()
        if s.endswith((".KS", ".KQ")):
            return s, "KRW"
        return s, "USD"

    if "CASH" in t or t.startswith("RP"):
        return None, "SKIP"

    if t == "KRW":
        return None, "FIXED"
    if t == "USD":
        return "USDKRW=X", "FX"
    if t == "GOLD":
        return "GC=F", "GOLD"

    if RE_KRX.match(t):
        return t, "KRW"

    if RE_US.match(t):
        return t, "USD"

    return None, "UNKNOWN"


# ─────────────────────────────────────────────
# 미국주식 — 프리/애프터마켓 (v2와 동일)
# ─────────────────────────────────────────────
def fetch_us_price(symbol: str):
    tk = yf.Ticker(symbol)

    try:
        info = tk.info or {}
        state = info.get("marketState", "")

        if state == "PRE" and info.get("preMarketPrice"):
            return float(info["preMarketPrice"]), "프리"

        if state in ("POST", "POSTPOST", "CLOSED", "PREPRE") and info.get("postMarketPrice"):
            return float(info["postMarketPrice"]), "애프터"

        if info.get("regularMarketPrice"):
            return float(info["regularMarketPrice"]), "정규"
    except Exception as e:
        log.warning(f"[{symbol}] info 조회 실패, 분봉 폴백: {e}")

    try:
        h = tk.history(period="1d", interval="1m", prepost=True)
        if len(h):
            return float(h["Close"].iloc[-1]), "확장"
    except Exception as e:
        log.warning(f"[{symbol}] 분봉 조회 실패, fast_info 폴백: {e}")

    try:
        return float(tk.fast_info.last_price), "기본"
    except Exception as e:
        log.warning(f"[{symbol}] 가격 조회 최종 실패: {e}")
        return None, ""


# ─────────────────────────────────────────────
# 국내주식 — NXT 운영시간 판별 (KST 기준)
#   프리: 08:00~08:50 / 메인: 09:00~15:20(KRX와 중복) / 애프터: 15:30~20:00
#   반환: "NXT프리" / "정규" / "NXT애프터" / None(장외)
# ─────────────────────────────────────────────
def krx_nxt_session():
    now = datetime.now(KST)
    if now.weekday() >= 5:        # 주말
        return None
    hm = now.hour * 100 + now.minute
    if 800 <= hm < 850:
        return "NXT프리"
    if 900 <= hm < 1530:
        return "정규"             # KRX 정규장 우선
    if 1530 <= hm < 2000:
        return "NXT애프터"
    return None


def _to_number(v):
    """네이버 응답 값('71,000' 같은 문자열 포함)을 숫자로 변환. 실패 시 None"""
    try:
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            s = v.replace(",", "").strip()
            if s and re.match(r"^-?\d+(\.\d+)?$", s):
                return float(s)
    except Exception:
        pass
    return None


def _walk_json(obj, path=""):
    """중첩 JSON을 (경로, 값) 쌍으로 평탄화"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_json(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_json(v, f"{path}[{i}]")
    else:
        yield path, obj


def _find_nxt_price(data: dict, krx_price: float):
    """
    네이버 응답 JSON에서 NXT 현재가 필드를 자동 탐색.
    경로에 'nxt'가 포함되고 키에 'price'가 포함된 숫자 값 중,
    KRX 가격 대비 ±30% 이내인 값을 NXT 현재가로 채택.
    """
    candidates = []
    for path, val in _walk_json(data):
        p = path.lower()
        if "nxt" not in p:
            continue
        leaf = p.split(".")[-1]
        if "price" not in leaf:
            continue
        # 고가/저가/시가/대비/누적류 제외
        if any(x in leaf for x in ("high", "low", "open", "compare", "accumulat", "fluctuat")):
            continue
        num = _to_number(val)
        if num and num > 0:
            # closePrice/tradePrice/currentPrice 우선
            score = 2 if any(x in leaf for x in ("close", "trade", "current")) else 1
            candidates.append((score, path, num))

    if not candidates:
        return None

    candidates.sort(key=lambda c: -c[0])
    for score, path, num in candidates:
        if krx_price and abs(num - krx_price) / krx_price <= 0.30:
            if DEBUG_NAVER:
                log.info(f"  NXT 필드 채택: {path} = {num}")
            return num
    return None


# ─────────────────────────────────────────────
# 국내주식 — 네이버페이 증권 API (NXT 반영)
#   반환: (가격 or None, 시장상태 라벨)
# ─────────────────────────────────────────────
def fetch_kr_price_naver(code: str):
    for endpoint in ("basic", "integration"):
        url = f"https://m.stock.naver.com/api/stock/{code}/{endpoint}"
        try:
            r = requests.get(url, headers=NAVER_HEADERS, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
        except Exception as e:
            log.warning(f"[{code}] 네이버 {endpoint} 조회 실패: {e}")
            continue

        if DEBUG_NAVER:
            keys = sorted({p for p, _ in _walk_json(data)})
            log.info(f"[{code}] 네이버 {endpoint} 응답 필드: {keys[:80]}")

        # KRX 현재가 (기본)
        krx_price = None
        for key in ("closePrice", "tradePrice", "currentPrice", "now"):
            if key in data:
                krx_price = _to_number(data[key])
                if krx_price:
                    break
        if krx_price is None:
            # 중첩 구조에서 nxt가 아닌 closePrice 탐색
            for path, val in _walk_json(data):
                p = path.lower()
                if "nxt" in p:
                    continue
                if p.split(".")[-1] in ("closeprice", "tradeprice", "currentprice"):
                    krx_price = _to_number(val)
                    if krx_price:
                        break
        if krx_price is None:
            continue

        # NXT 운영시간이면 NXT 가격 우선 적용
        session = krx_nxt_session()
        if session in ("NXT프리", "NXT애프터"):
            nxt_price = _find_nxt_price(data, krx_price)
            if nxt_price:
                return nxt_price, session
            return krx_price, "정규(NXT미발견)"

        return krx_price, session or "장마감"

    return None, ""


# ─────────────────────────────────────────────
# 국내주식 — yfinance 폴백 (.KS → .KQ)
# ─────────────────────────────────────────────
def fetch_krx_price_yf(code: str):
    for suffix in (".KS", ".KQ"):
        sym = code + suffix
        try:
            p = float(yf.Ticker(sym).fast_info.last_price)
            if p and p > 0:
                return p
        except Exception:
            continue
    log.warning(f"[{code}] yfinance .KS/.KQ 모두 조회 실패")
    return None


# ─────────────────────────────────────────────
# 가격 조회 통합
# ─────────────────────────────────────────────
def fetch_price(ticker: str, symbol, currency: str) -> dict:
    if currency == "FIXED":
        return {"krw": 1, "usd": None, "state": ""}

    if currency == "USD":
        price, state = fetch_us_price(symbol)
        if price is None:
            return {"krw": None, "usd": None, "state": ""}
        rate = get_usd_krw()
        return {"krw": round(price * rate), "usd": round(price, 4), "state": state}

    if currency == "GOLD":
        price, _ = fetch_us_price(symbol)
        if price is None:
            return {"krw": None, "usd": None, "state": ""}
        g = price / 31.1035  # oz → g
        rate = get_usd_krw()
        return {"krw": round(g * rate), "usd": round(g, 4), "state": ""}

    if currency == "FX":
        return {"krw": round(get_usd_krw(), 2), "usd": None, "state": ""}

    if currency == "KRW":
        # '심볼' 컬럼에 .KS/.KQ를 직접 지정한 경우 → yfinance 직행
        if symbol.endswith((".KS", ".KQ")):
            code = symbol[:-3]
        else:
            code = symbol

        # 1차: 네이버 (NXT 프리/애프터 반영)
        price, state = fetch_kr_price_naver(code)
        if price:
            return {"krw": round(price), "usd": None, "state": state}

        # 2차: yfinance 폴백
        price = fetch_krx_price_yf(code)
        if price:
            return {"krw": round(price), "usd": None, "state": "yf폴백"}

        return {"krw": None, "usd": None, "state": ""}

    return {"krw": None, "usd": None, "state": ""}


# ─────────────────────────────────────────────
# 노션 DB 스키마 확인 (선택 컬럼 존재 여부)
# ─────────────────────────────────────────────
def check_optional_columns():
    global HAS_MARKET_STATE_COL, HAS_SYMBOL_COL
    try:
        r = requests.get(
            f"https://api.notion.com/v1/databases/{NOTION_DB_ID}",
            headers=HEADERS,
        )
        r.raise_for_status()
        props = r.json().get("properties", {})
        HAS_MARKET_STATE_COL = "시장상태" in props
        HAS_SYMBOL_COL = "심볼" in props
        log.info(f"선택 컬럼 — 시장상태: {HAS_MARKET_STATE_COL}, 심볼: {HAS_SYMBOL_COL}")
    except Exception as e:
        log.warning(f"DB 스키마 조회 실패 (선택 컬럼 미사용): {e}")


# ─────────────────────────────────────────────
# 노션 DB 전체 행 조회 (페이지네이션 지원)
# ─────────────────────────────────────────────
def get_pages() -> list:
    pages, payload = [], {}
    while True:
        r = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
            headers=HEADERS, json=payload,
        )
        r.raise_for_status()
        data = r.json()
        pages.extend(data["results"])
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]
    return pages


# ─────────────────────────────────────────────
# 노션 현재가 업데이트
# ─────────────────────────────────────────────
def update_page(page_id: str, krw, usd, state: str):
    props = {}
    if krw is not None:
        props["현재가"] = {"number": krw}
    if usd is not None:
        props["현재가(USD)"] = {"number": usd}
    if HAS_MARKET_STATE_COL and state:
        props["시장상태"] = {
            "rich_text": [{"text": {"content": state}}]
        }
    if not props:
        return
    r = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=HEADERS, json={"properties": props},
    )
    r.raise_for_status()


def get_text_prop(page: dict, name: str) -> str:
    prop = page["properties"].get(name, {})
    arr = prop.get("title") or prop.get("rich_text") or []
    return arr[0]["plain_text"].strip() if arr else ""


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    log.info(f"===== 업데이트 시작: {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST =====")

    check_optional_columns()
    pages = get_pages()
    log.info(f"{len(pages)}개 행 로드 완료")

    price_cache: dict = {}
    ok = fail = skip = 0

    for page in pages:
        ticker = get_text_prop(page, "티커")
        if not ticker:
            skip += 1
            continue

        override = get_text_prop(page, "심볼") if HAS_SYMBOL_COL else ""
        symbol, currency = resolve_ticker(ticker, override)

        if currency == "SKIP":
            skip += 1
            continue

        if currency == "UNKNOWN":
            log.warning(f"[{ticker}] 형식을 인식할 수 없음 → 건너뜀 "
                        f"(필요 시 노션 '심볼' 컬럼에 yfinance 심볼을 직접 입력)")
            skip += 1
            continue

        cache_key = f"{ticker}|{symbol}"
        if cache_key in price_cache:
            prices = price_cache[cache_key]
        else:
            prices = fetch_price(ticker, symbol, currency)
            price_cache[cache_key] = prices
            time.sleep(0.3)

        if prices["krw"] is None and prices["usd"] is None:
            fail += 1
            continue

        try:
            update_page(page["id"], prices["krw"], prices["usd"], prices.get("state", ""))
            parts = []
            if prices["krw"]:
                parts.append(f"₩{prices['krw']:,}")
            if prices["usd"]:
                parts.append(f"${prices['usd']}")
            if prices.get("state"):
                parts.append(f"({prices['state']})")
            log.info(f"[{ticker}] ✅ {' / '.join(parts)}")
            ok += 1
        except Exception as e:
            log.error(f"[{ticker}] 노션 업데이트 실패: {e}")
            fail += 1

    log.info(f"===== 완료 — 성공:{ok} 실패:{fail} 스킵:{skip} =====")


if __name__ == "__main__":
    main()
