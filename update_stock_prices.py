#!/usr/bin/env python3
"""
Woomi 증권 종합 잔고 — 현재가 자동 업데이트
GitHub Actions에서 5분마다 실행됩니다 (cron-job.org → repository_dispatch).

DB 구조:
- 종목별 + 계좌별로 행이 분리됨 (예: NVDA-우재, NVDA-혜미, NVDA-하성)
- 동일 티커의 모든 행은 같은 현재가로 업데이트됨 (메인 루프가 페이지 단위로 순회)
- 현금 행(*-CASH)은 yfinance 조회 대상 아님 → SKIP 처리
"""

import os
import time
import logging
from datetime import datetime

import requests
import yfinance as yf

# ─────────────────────────────────────────────
# 환경변수 (GitHub Secrets에서 주입)
# ─────────────────────────────────────────────
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_ID = os.environ.get("NOTION_DB_ID", "2b7bf6e4578e802683b8f3e28bc9f61b")

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 티커 설정
#   symbol   : yfinance 심볼 (없으면 None)
#   currency : USD / KRW / FX(환율) / GOLD / FIXED / SKIP
# ─────────────────────────────────────────────
TICKERS = {
    # 해외주식 (USD)
    "TSLA":   {"symbol": "TSLA",      "currency": "USD"},
    "NVDA":   {"symbol": "NVDA",      "currency": "USD"},
    "UPS":    {"symbol": "UPS",       "currency": "USD"},
    "TEM":    {"symbol": "TEM",       "currency": "USD"},

    # 금 (1g 환산)
    "GOLD":   {"symbol": "GC=F",      "currency": "GOLD"},

    # 국내주식 / ETF (KRW)
    "035900": {"symbol": "035900.KQ", "currency": "KRW"},  # JYP Ent (코스닥)
    "381170": {"symbol": "381170.KS", "currency": "KRW"},  # TIGER 미국테크TOP10
    "457480": {"symbol": "457480.KS", "currency": "KRW"},  # ACE 테슬라밸류체인
    "447770": {"symbol": "447770.KS", "currency": "KRW"},  # TIGER 테슬라채권혼합
    "0052S0": {"symbol": "0052S0.KS", "currency": "KRW"},  # 1Q S&P500혼합50
"000660": {"symbol": "000660.KS", "currency": "KRW"},  # SK하이닉스
    "483320": {"symbol": "483320.KS", "currency": "KRW"},  # ACE 엔비디아밸류체인액티브
    "448540": {"symbol": "448540.KS", "currency": "KRW"},  # ACE 엔비디아채권혼합

    # 환율 / 고정
    "USD":    {"symbol": "USDKRW=X",  "currency": "FX"},
    "KRW":    {"symbol": None,        "currency": "FIXED", "fixed": 1},

    # 현금 항목 — 수동 관리, 자동 업데이트 대상 아님
    "KRW-CASH": {"symbol": None, "currency": "SKIP"},
    "USD-CASH": {"symbol": None, "currency": "SKIP"},
    "DC-CASH":  {"symbol": None, "currency": "SKIP"},
}

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
# 가격 조회
# ─────────────────────────────────────────────
def fetch_price(ticker: str, info: dict) -> dict:
    currency = info["currency"]

    # 현금 항목 — yfinance 조회 안 함
    if currency == "SKIP":
        return {"krw": None, "usd": None}

    # 고정값 (예: KRW=1)
    if currency == "FIXED":
        return {"krw": info["fixed"], "usd": None}

    try:
        price = float(yf.Ticker(info["symbol"]).fast_info.last_price)
    except Exception as e:
        log.warning(f"[{ticker}] 가격 조회 실패: {e}")
        return {"krw": None, "usd": None}

    usd_krw = get_usd_krw()

    if currency == "USD":
        return {"krw": round(price * usd_krw), "usd": round(price, 4)}

    elif currency == "GOLD":
        # oz → g 변환 (1 troy oz = 31.1035g)
        g = price / 31.1035
        return {"krw": round(g * usd_krw), "usd": round(g, 4)}

    elif currency == "FX":
        return {"krw": round(price, 2), "usd": None}

    elif currency == "KRW":
        return {"krw": round(price), "usd": None}

    return {"krw": None, "usd": None}


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
def update_page(page_id: str, krw, usd):
    props = {}
    if krw is not None:
        props["현재가"] = {"number": krw}
    if usd is not None:
        props["현재가(USD)"] = {"number": usd}
    if not props:
        return
    r = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=HEADERS, json={"properties": props},
    )
    r.raise_for_status()


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    log.info(f"===== 업데이트 시작: {datetime.now().strftime('%Y-%m-%d %H:%M')} =====")

    pages = get_pages()
    log.info(f"{len(pages)}개 행 로드 완료")

    # 같은 티커는 가격을 1번만 조회해서 재사용 (yfinance 호출 절감)
    price_cache: dict = {}

    ok = fail = skip = 0

    for page in pages:
        title_arr = page["properties"].get("티커", {}).get("title", [])
        ticker = title_arr[0]["plain_text"].strip() if title_arr else ""

        if not ticker:
            skip += 1
            continue

        info = TICKERS.get(ticker)
        if not info:
            log.warning(f"[{ticker}] 매핑 없음 → 건너뜀")
            skip += 1
            continue

        # SKIP 항목(현금)은 가격 조회/업데이트 모두 스킵 — 조용히 패스
        if info["currency"] == "SKIP":
            skip += 1
            continue

        # 가격 캐시 활용
        if ticker in price_cache:
            prices = price_cache[ticker]
        else:
            prices = fetch_price(ticker, info)
            price_cache[ticker] = prices
            # 첫 조회 후에만 sleep (캐시 적중 시 sleep 불필요)
            time.sleep(0.3)

        if prices["krw"] is None and prices["usd"] is None:
            fail += 1
            continue

        try:
            update_page(page["id"], prices["krw"], prices["usd"])
            parts = []
            if prices["krw"]:
                parts.append(f"₩{prices['krw']:,}")
            if prices["usd"]:
                parts.append(f"${prices['usd']}")
            log.info(f"[{ticker}] ✅ {' / '.join(parts)}")
            ok += 1
        except Exception as e:
            log.error(f"[{ticker}] 노션 업데이트 실패: {e}")
            fail += 1

    log.info(f"===== 완료 — 성공:{ok} 실패:{fail} 스킵:{skip} =====")


if __name__ == "__main__":
    main()
