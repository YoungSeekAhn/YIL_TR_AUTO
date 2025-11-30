# -*- coding: utf-8 -*-
"""
KIS 자동 트레이딩 시스템 (ULTRA VERSION) - Part 3
───────────────────────────────────────────────
- Stage 1 (08:50): CSV 로드 + 필터 + 보유 반영 + GUI/WATCHER 기동
- PriceWatcher:
    · 장중(09:00~15:30) 현재가 감시
    · 종목당 매수 1회 + 매도(TP/SL/만기청산) 1회 원칙
    · 손절 발생 시 그날 해당 종목 완전 제외 (재매수 금지)
    · 주문 실패 시 error_lock 걸고 당일 거래 중지 (무한 재시도 방지)
"""

import os, sys, time, threading, requests, queue
from datetime import datetime, timezone, timedelta, time as dtime
from pathlib import Path

import pandas as pd
import schedule
from dotenv import find_dotenv, load_dotenv

from auto_tr_functions import last_trading_day, last_report_day
from TRConfig import config
from auto_tr_info import AlertManager, LogManager, KISClient
from auto_tr_gui import TraderGUIUltra, SharedState

# ──────────────────────────────────────────────────────────────
# 공통 상수/환경
# ──────────────────────────────────────────────────────────────

# Korea Standard Time (UTC+9)
KST = timezone(timedelta(hours=9), name="KST")

# 거래시간
MARKET_START = dtime(9, 0)
MARKET_END   = dtime(15, 30)

# 필요시 환경변수 기반 파라미터(없으면 기본값 사용)
THROTTLE_SEC = float(os.getenv("THROTTLE_SEC", 3.0))

REAL_BASE  = "https://openapi.koreainvestment.com:9443"
PAPER_BASE = "https://openapivts.koreainvestment.com:29443"

# 전역 로그 큐 (GUI 로그창 연동용)
_log_queue_singleton = None
def _global_log_queue():
    global _log_queue_singleton
    if _log_queue_singleton is None:
        _log_queue_singleton = queue.Queue()
    return _log_queue_singleton


# ──────────────────────────────────────────────────────────────
# Helper: 주문 성공 여부 판별
# ──────────────────────────────────────────────────────────────
def is_order_success(resp: dict) -> bool:
    """
    KIS 주문 응답에서 성공 여부 판단
    - 일반적으로 rt_cd == '0' 이면 정상
    """
    if not isinstance(resp, dict):
        return False
    rt = str(resp.get("rt_cd", "")).strip()
    return (rt == "0")


# ──────────────────────────────────────────────────────────────
# 가격 감시/주문 실행기
# ──────────────────────────────────────────────────────────────
class PriceWatcher:
    """
    - 장중에만 동작 (09:00~15:30)
    - 현재가 조회 → 개별 P/L 계산 → 조건 충족 시 주문
    - 정책:
        · 종목당 매수는 1일 1회
        · 종목당 매도(TP/SL/만기청산)는 1일 1회
        · 손절 발생 시: 그날 해당 종목은 완전 제외 (재매수 금지)
        · 주문 실패 시: error_lock 걸고 당일 해당 종목 거래 중단
    """

    def __init__(self, df, kis: KISClient, shared: SharedState,
                 alerts: AlertManager, logs: LogManager, gui_ref: TraderGUIUltra):
        self.df = df.reset_index(drop=True)
        self.kis = kis
        self.shared = shared
        self.alerts = alerts
        self.logs = logs
        self.gui = gui_ref
        self.running = True

        # Meta 캐시 + 상태 플래그
        self.meta = {}
        for i, r in self.df.iterrows():
            code = str(r["종목코드"]).zfill(6)
            side = str(r.get("side") or "").upper()
            qty_val = int(float(r.get("ord_qty") or 0))

            # valid_until ISO8601 → datetime 변환
            valid_until_raw = r.get("valid_until")
            valid_until_dt = None
            if isinstance(valid_until_raw, str) and valid_until_raw.strip():
                try:
                    valid_until_dt = datetime.fromisoformat(valid_until_raw)
                    # tz 정보 없으면 KST 부여
                    if valid_until_dt.tzinfo is None:
                        valid_until_dt = valid_until_dt.replace(tzinfo=KST)
                except Exception:
                    valid_until_dt = None

            self.meta[code] = {
                "name": r.get("종목명", ""),
                "qty": qty_val,
                "entry": float(r.get("매수가(entry)") or r.get("last_close") or 0),
                "tp": float(r.get("익절가(tp)") or 0),
                "sl": float(r.get("손절가(sl)") or 0),
                "side": side,
                "valid_until": valid_until_dt,
                # ✅ 상태 플래그
                "bought": False,     # 오늘 매수 여부
                "sold": False,       # 오늘 매도(TP/SL/만기청산) 여부
                "error_lock": False, # 주문 오류로 인한 당일 거래 정지 여부
            }

        # 이미 보유 중인 종목(side=SELL) → 매수 완료 상태로 간주(매도만 수행)
        for code, m in self.meta.items():
            if m["side"] == "SELL" and m["qty"] > 0:
                m["bought"] = True   # 이미 들고 있는 물량 → 새로 매수할 필요 없음

        # 총 평가 기준액(매수가×수량 합) 계산 → SharedState에 초기 세팅
        total_base = 0.0
        for code, m in self.meta.items():
            if m["entry"] > 0 and m["qty"] > 0:
                total_base += m["entry"] * m["qty"]
        self.shared.set_totals(total_pl=0.0, total_base=total_base)

    def _in_market(self) -> bool:
        now_t = datetime.now(KST).time()
        return (MARKET_START <= now_t <= MARKET_END)

    def monitor(self):
        self._log("[Stage 2] 감시 시작")
        while self.running:
            if not self._in_market():
                self._log("[STOP] 거래시간 종료")
                break

            total_pl = 0.0
            total_base = 0.0

            for idx, r in self.df.iterrows():
                code = str(r["종목코드"]).zfill(6)
                if code not in self.meta:
                    continue
                m = self.meta[code]

                # error_lock 종목은 당일 거래 완전 중단
                if m["error_lock"]:
                    continue

                qty, entry, tp, sl, side = m["qty"], m["entry"], m["tp"], m["sl"], m["side"]

                # 수량 0 또는 코드 비정상시 스킵
                if qty <= 0 or not code:
                    continue

                # 시세 조회
                try:
                    price = self.kis.get_price(code)
                except Exception as e:
                    self._log(f"[WARN] {m['name']} 가격 조회 실패: {e}")
                    continue

                # 개별 손익 계산
                pl = (price - entry) * qty if entry > 0 else 0.0
                self.shared.update_symbol(code, price=price, pl=pl)
                total_pl += pl
                if entry > 0:
                    total_base += entry * qty

                # 이미 매도까지 끝난 종목이면 이후 아무 작업도 안 함
                if m["sold"]:
                    continue

                now = datetime.now(KST)

                # 0) 유효기간(valid_until) 경과 시 강제 청산 (시장가)
                if m["bought"] and not m["sold"] and m["valid_until"] is not None:
                    if now >= m["valid_until"]:
                        self._log(f"[EXP SELL] {m['name']} 유효기간 경과 → 강제 청산(시장가)")
                        resp = self.kis.place_order("SELL", code, qty, 0, ord_dvsn="01")  # 01 = 시장가
                        if not is_order_success(resp):
                            self._log(f"[ERROR] 만기청산 주문 실패: {m['name']} resp={resp}")
                            m["error_lock"] = True
                            self.logs.record(m["name"], "SELL(EXP-FAIL)", qty, price, pl, note=str(resp))
                            continue
                        m["sold"] = True
                        self.shared.update_symbol(code, status="expired_sold")
                        self.gui.toast(f"만기 청산: {m['name']} (시장가)", bg="#455a64")
                        self.alerts.notify("만기 청산", f"{m['name']} {qty}주 시장가 청산 (현재가 {int(price)})")
                        self.logs.record(m["name"], "SELL(EXP)", qty, price, pl, note=str(resp))
                        continue

                # 1) 매수 조건 (BUY: 현재가 <= 매수가) — 아직 매수 안 했을 때만
                if (
                    side == "BUY"
                    and entry > 0
                    and price <= entry
                    and (not m["bought"])
                ):
                    self._log(f"[BUY] {m['name']} {price:.0f} ≤ {entry:.0f}")
                    resp = self.kis.place_order("BUY", code, qty, int(entry), ord_dvsn="00")  # 00=지정가
                    if not is_order_success(resp):
                        self._log(f"[ERROR] 매수 주문 실패: {m['name']} resp={resp}")
                        # 주문 실패 시 해당 종목은 당일 거래 중단
                        m["error_lock"] = True
                        self.logs.record(m["name"], "BUY-FAIL", qty, entry, 0.0, note=str(resp))
                        continue
                    # 성공 시
                    m["bought"] = True   # 오늘 매수 완료
                    self.shared.update_symbol(code, status="bought")
                    self.logs.record(m["name"], "BUY", qty, entry, 0.0, note=str(resp))
                    # 매수 직후에는 TP/SL 바로 쏘지 않고 다음 루프에서 판단
                    continue

                # 매수 안된 종목은 TP/SL/만기청산 대상이 아님
                if not m["bought"]:
                    continue

                # 2) 익절 조건 (현재가 ≥ TP) — 매수 후, 아직 매도 안 했을 때만
                if tp > 0 and price >= tp and not m["sold"]:
                    self._log(f"[TP SELL] {m['name']} {price:.0f} ≥ {tp:.0f}")
                    resp = self.kis.place_order("SELL", code, qty, int(tp), ord_dvsn="00")
                    if not is_order_success(resp):
                        self._log(f"[ERROR] 익절 주문 실패: {m['name']} resp={resp}")
                        m["error_lock"] = True
                        self.logs.record(m["name"], "SELL(TP-FAIL)", qty, tp, pl, note=str(resp))
                        continue
                    m["sold"] = True   # 오늘 매도 완료
                    self.shared.update_symbol(code, status="tp_sold")
                    self.gui.toast(f"익절 체결: {m['name']} ({price:.0f})", bg="#2e7d32")
                    self.alerts.notify("익절 체결", f"{m['name']} {qty}주 @ {int(tp)} (현재가 {int(price)})")
                    self.logs.record(m["name"], "SELL(TP)", qty, tp, pl, note=str(resp))
                    continue

                # 3) 손절 조건 (현재가 ≤ SL) — 매수 후, 아직 매도 안 했을 때만
                if sl > 0 and price <= sl and not m["sold"]:
                    self._log(f"[SL SELL] {m['name']} {price:.0f} ≤ {sl:.0f}")
                    resp = self.kis.place_order("SELL", code, qty, int(sl), ord_dvsn="00")
                    if not is_order_success(resp):
                        self._log(f"[ERROR] 손절 주문 실패: {m['name']} resp={resp}")
                        m["error_lock"] = True  # 재시도 무한루프 방지
                        self.logs.record(m["name"], "SELL(SL-FAIL)", qty, sl, pl, note=str(resp))
                        continue
                    m["sold"] = True
                    self.shared.update_symbol(code, status="sl_sold")
                    self.gui.toast(f"손절 체결: {m['name']} ({price:.0f})", bg="#c62828")
                    self.alerts.notify("손절 체결", f"{m['name']} {qty}주 @ {int(sl)} (현재가 {int(price)})")
                    self.logs.record(m["name"], "SELL(SL)", qty, sl, pl, note=str(resp))
                    # ✅ 손절 후 그날 해당 종목 완전 제외: m["sold"]=True + error_lock는 X
                    #   (다시 매수조건을 보더라도 m["bought"]와 m["sold"] 때문에 추가매매 없음)
                    continue

            # 총합 반영
            self.shared.set_totals(total_pl=total_pl, total_base=total_base)
            time.sleep(THROTTLE_SEC)

    def _log(self, msg: str):
        now = datetime.now(KST).strftime("[%H:%M:%S] ")
        print(now + msg)


# ──────────────────────────────────────────────────────────────
# 잔고 조회: 보유 종목 코드 리스트
# ──────────────────────────────────────────────────────────────
def inquire_holdings_codes(kis: KISClient):
    """
    보유 종목 코드 목록을 간단히 조회
    - 실제 엔드포인트/파라미터는 KIS 문서에 따라 조정 필요
    """
    try:
        url = f"{kis.base}/uapi/domestic-stock/v1/trading/inquire-balance"
        headers = {
            "authorization": f"Bearer {kis.tok.get()}",
            "appkey": kis.appkey,
            "appsecret": kis.appsecret,
            "tr_id": "TTTC8434R" if not kis.is_paper else "VTTC8434R",
        }
        params = {
            "CANO": kis.acct8,
            "ACNT_PRDT_CD": kis.prod2,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
        }
        r = requests.get(url, headers=headers, params=params, timeout=10)
        out = r.json().get("output1", []) if r.text else []
        return [i.get("pdno") for i in out if "pdno" in i]
    except Exception as e:
        print(f"[WARN] 잔고조회 실패: {e}")
        return []


# ──────────────────────────────────────────────────────────────
# Stage 1: 08:50 — CSV 로드 + 필터(Score/RR/conf) + 보유 반영 + GUI/WATCHER 기동
# ──────────────────────────────────────────────────────────────
def pre_market_stage():
    # .env 로드
    env_path = Path(find_dotenv(usecwd=True))
    load_dotenv(dotenv_path=str(env_path), override=True)

    # 실/모의 환경, 인증 정보
    is_paper = True if os.getenv("KIS_USE_PAPER", "1").lower() not in ("0", "false") else False
    appkey    = os.getenv("KIS_APPKEY_VTS" if is_paper else "KIS_APPKEY")
    appsecret = os.getenv("KIS_APPSECRET_VTS" if is_paper else "KIS_APPSECRET")
    account   = os.getenv("KIS_ACCOUNT_VTS" if is_paper else "KIS_ACCOUNT")
    base      = PAPER_BASE if is_paper else REAL_BASE

    if not all([appkey, appsecret, account]):
        print("[ERROR] .env 인증값 누락"); return

    # 리포트 일자/경로
    config.end_date = last_report_day()
    csv_path = Path(config.price_report_dir) / f"Report_{config.end_date}" / f"Trading_price_{config.end_date}.csv"
    if not csv_path.exists():
        print(f"[ERROR] CSV 없음: {csv_path}"); return

    # CSV 로드
    df = pd.read_csv(csv_path, dtype={"종목코드": str})

    # 숫자 변환
    for col in ("confidence", "RR", "Score_1w"):
        if col not in df.columns:
            print(f"[ERROR] CSV에 '{col}' 컬럼이 없습니다."); return
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # --- 매매대상 조건 ---
    MIN_SCORE = 140.0
    MIN_RR    = 2.5
    MIN_CONF  = 0.45

    df = df[(df["Score_1w"] >= MIN_SCORE) & (df["RR"] >= MIN_RR) & (df["confidence"] >= MIN_CONF)].copy()
    if df.empty:
        print(f"[INFO] 조건 충족 종목 없음 (Score_1w≥{MIN_SCORE}, RR≥{MIN_RR}, confidence≥{MIN_CONF})")
        return

    # 정렬: Score_1w ↓, RR ↓, confidence ↓ (stable)
    df = df.sort_values(["Score_1w", "RR", "confidence"],
                        ascending=[False, False, False],
                        kind="mergesort")

    # KIS 클라이언트/알림/로그/공유상태
    kis = KISClient(base, appkey, appsecret, account, is_paper)
    alerts = AlertManager()
    logs = LogManager()
    shared = SharedState()

    # 보유 종목은 side=SELL로 전환
    try:
        held_codes = inquire_holdings_codes(kis)
    except Exception:
        held_codes = []
    if len(held_codes) > 0 and "side" in df.columns:
        df.loc[df["종목코드"].isin(held_codes), "side"] = "SELL"

    # GUI 생성
    gui = TraderGUIUltra(df, shared, log_queue=_global_log_queue())

    # 장 시작(09:00)까지 대기 후 감시 시작 (별도 스레드)
    def delayed_start():
        now = datetime.now(KST)
        start_dt = datetime.combine(now.date(), MARKET_START, tzinfo=KST)
        wait = (start_dt - now).total_seconds()
        if wait > 0:
            print(f"[SYSTEM] 장 시작까지 {int(wait)}초 대기...")
            time.sleep(wait + 30)

        watcher = PriceWatcher(df, kis, shared, alerts, logs, gui)
        watcher.monitor()

    threading.Thread(target=delayed_start, daemon=True).start()
    gui.run()


# ──────────────────────────────────────────────────────────────
# 메인: (지금은 바로 pre_market_stage 실행)
# ──────────────────────────────────────────────────────────────
def main():
    schedule.clear()
    print("[SYSTEM] ULTRA GUI 버전 시작 (TEST: 즉시 pre_market_stage 실행)")
    pre_market_stage()
    # 실제 스케줄 사용 시:
    # schedule.every().day.at("08:50").do(pre_market_stage)
    # while True:
    #     schedule.run_pending()
    #     time.sleep(10)


if __name__ == "__main__":
    main()