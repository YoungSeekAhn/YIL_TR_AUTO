# ──────────────────────────────────────────────────────────────
# Part 3 — PriceWatcher + Stage + Main (통합 실행)
# ──────────────────────────────────────────────────────────────
import pandas as pd
import schedule
from dotenv import find_dotenv, load_dotenv

import os, sys, time, threading, requests
from datetime import datetime, timezone, timedelta

# Korea Standard Time (UTC+9) timezone object for consistent localized timestamps
KST = timezone(timedelta(hours=9), name="KST")

from datetime import time as dtime
from pathlib import Path

from auto_tr_functions import last_trading_day, last_report_day
from TRConfig import config
from auto_tr_info import AlertManager, LogManager, KISClient
from auto_tr_gui import TraderGUIUltra, SharedState

# 거래시간
MARKET_START = dtime(9, 0)
MARKET_END   = dtime(15, 30)

# 필요시 환경변수 기반 파라미터(없으면 기본값 사용)
THROTTLE_SEC = float(os.getenv("THROTTLE_SEC", 3.0))
MIN_CONF     = float(os.getenv("MIN_CONF", 0.5))

REAL_BASE  = "https://openapi.koreainvestment.com:9443"
PAPER_BASE = "https://openapivts.koreainvestment.com:29443"


# ──────────────────────────────────────────────────────────────
# 가격 감시/주문 실행기
# ──────────────────────────────────────────────────────────────
class PriceWatcher:
    """
    - 장중에만 동작 (09:00~15:30)
    - 현재가 조회 → 개별 P/L 계산 → 조건 충족 시 주문
    - 익절/손절시: 주문 + Toast + 이메일/카카오 알림 + CSV 로그 기록
    - 총 P/L, EquityCurve를 SharedState에 반영 → GUI가 실시간 표시
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

        # Meta 캐시
        self.meta = {}
        for i, r in self.df.iterrows():
            code = str(r["종목코드"]).zfill(6)
            self.meta[code] = {
                "name": r.get("종목명", ""),
                "qty": int(float(r.get("ord_qty") or 0)),
                "entry": float(r.get("매수가(entry)") or r.get("last_close") or 0),
                "tp": float(r.get("익절가(tp)") or 0),
                "sl": float(r.get("손절가(sl)") or 0),
                "side": str(r.get("side") or "").upper(),
            }

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
                m = self.meta[code]
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

                # 주문 트리거
                # 1) 매수 조건 (BUY: 현재가 <= 매수가)
                if side == "BUY" and entry > 0 and price <= entry and r.get("status") not in ("bought", "tp_sold", "sl_sold"):
                    self._log(f"[BUY] {m['name']} {price:.0f} ≤ {entry:.0f}")
                    resp = self.kis.place_order("BUY", code, qty, int(entry), ord_dvsn="00")
                    r["status"] = "bought"
                    self.shared.update_symbol(code, status="bought")
                    self.logs.record(m["name"], "BUY", qty, entry, 0.0, note=str(resp))

                # 2) 익절 조건 (현재가 ≥ TP)
                if tp > 0 and price >= tp and r.get("status") != "tp_sold":
                    self._log(f"[TP SELL] {m['name']} {price:.0f} ≥ {tp:.0f}")
                    resp = self.kis.place_order("SELL", code, qty, int(tp), ord_dvsn="00")
                    r["status"] = "tp_sold"
                    self.shared.update_symbol(code, status="tp_sold")
                    self.gui.toast(f"익절 체결: {m['name']} ({price:.0f})", bg="#2e7d32")
                    self.alerts.notify("익절 체결", f"{m['name']} {qty}주 @ {int(tp)} (현재가 {int(price)})")
                    self.logs.record(m["name"], "SELL(TP)", qty, tp, pl, note=str(resp))

                # 3) 손절 조건 (현재가 ≤ SL)
                elif sl > 0 and price <= sl and r.get("status") != "sl_sold":
                    self._log(f"[SL SELL] {m['name']} {price:.0f} ≤ {sl:.0f}")
                    resp = self.kis.place_order("SELL", code, qty, int(sl), ord_dvsn="00")
                    r["status"] = "sl_sold"
                    self.shared.update_symbol(code, status="sl_sold")
                    self.gui.toast(f"손절 체결: {m['name']} ({price:.0f})", bg="#c62828")
                    self.alerts.notify("손절 체결", f"{m['name']} {qty}주 @ {int(sl)} (현재가 {int(price)})")
                    self.logs.record(m["name"], "SELL(SL)", qty, sl, pl, note=str(resp))

            # 총합 반영 (EquityCurve 갱신)
            self.shared.set_totals(total_pl=total_pl, total_base=total_base)
            time.sleep(THROTTLE_SEC)

    def _log(self, msg: str):
        now = datetime.now(KST).strftime("[%H:%M:%S] ")
        print(now + msg)
        

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

    # --- Score_1w 보강: 없으면 scored_{end_date}.csv에서 병합 ---
    def _norm_code(s: pd.Series) -> pd.Series:
        s = s.astype(str).str.strip()
        s = s.str.replace(r"\.0+$", "", regex=True).str.replace("-", "", regex=False)
        return s.apply(lambda x: x.zfill(6) if x.isdigit() else x)

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


def inquire_holdings_codes(kis: KISClient):
    """
    보유 종목 코드 목록 (간단 구현: 잔고조회 REST가 Part1에 없으면 get_holdings 추가 필요)
    - 여기서는 간단히 주문조회/잔고조회 API가 있다고 가정하고 구현합니다.
    """
    try:
        # 간단 잔고조회 endpoint (문서별 상이할 수 있음)
        url = f"{kis.base}/uapi/domestic-stock/v1/trading/inquire-balance"
        headers = {"authorization": f"Bearer {kis.tok.get()}",
                   "appkey": kis.appkey, "appsecret": kis.appsecret,
                   "tr_id": "TTTC8434R"}
        params = {"CANO": kis.acct8, "ACNT_PRDT_CD": kis.prod2,
                  "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01"}
        r = requests.get(url, headers=headers, params=params, timeout=10)
        out = r.json().get("output1", []) if r.text else []
        return [i.get("pdno") for i in out if "pdno" in i]
    except Exception:
        return []


# ──────────────────────────────────────────────────────────────
# 전역 로그 큐 (GUI 로그창 연동용) — 필요 시 확장 가능
# ──────────────────────────────────────────────────────────────
import queue
_log_queue_singleton = None
def _global_log_queue():
    global _log_queue_singleton
    if _log_queue_singleton is None:
        _log_queue_singleton = queue.Queue()
    return _log_queue_singleton


# ──────────────────────────────────────────────────────────────
# 메인: 매일 08:50 Stage 1 실행
# ──────────────────────────────────────────────────────────────
def main():
    schedule.clear()
    #schedule.every().day.at("08:50").do(pre_market_stage)
    print("[SYSTEM] ULTRA GUI 버전 시작 (매일 08:50 자동 실행)")
    pre_market_stage()  # 일단 즉시 실행해봄
    while True:
        schedule.run_pending()
        time.sleep(10)


if __name__ == "__main__":
    main()

# py -m PyInstaller --onefile auto_tr_main.py