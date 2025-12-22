"""
yil_scheduler.py

한국 주식시장(09:00~15:30)을 기준으로
하루 동안 자동으로 다음을 수행하는 스케줄러:

- 08:30 : CSV 시그널 미리 로딩 (파싱 오류 체크, DB 준비)
- 09:00 : 신규 매수 + TP 지정가 예약매도 실행
- 09:00~15:15 : SL 조건 주기적으로 체크 → 조건 맞으면 시장가 손절
- 15:30 : 장 마감 후 한 번 더 동기화(SL/TP_SYNC) 후 마감 정리
- 16:00 : 프로그램 종료

실행 예:
    python yil_scheduler.py signals_2025-12-02.csv
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from typing import List, Optional
from TRConfig import config
from kis_functions import last_report_day
from kis_trading import (
    KISAPI,
    load_signals_from_csv,
    open_new_positions_from_signals,
    adjust_signals_based_on_trends,
    open_new_positions_from_signals,
    process_open_positions,
    expire_pending_orders,
    sync_pending_to_open,
    force_close_positions_1515_1530,
)
from kis_pos_db import DB_PATH, init_db


# ------------------------------------------------------------
# 설정값
# ------------------------------------------------------------

# SL 체크 주기(초) – 너무 짧으면 트래픽 부담, 너무 길면 손절 지연
SL_CHECK_INTERVAL_SEC = 60  # 1분마다

# 하루 종료 시각 (로컬 시간 기준)
EOD_EXIT_TIME = dtime(16, 0)  # 16:00 이후 종료

# =========================
# Scheduler constants
# =========================
CHECK_INTERVAL_SEC = 30          # 장중 동기화/TP/SL 체크 주기
EOD_EXIT_TIME = dtime(16, 0)     # 16:00 이후 종료

# ------------------------------------------------------------
# 헬퍼
# ------------------------------------------------------------

def now_time() -> dtime:
    """현재 로컬 시각의 time 객체 반환."""
    return datetime.now().time()


def between(t: dtime, start: dtime, end: dtime) -> bool:
    """t 가 [start, end) 구간에 들어있는지 여부."""
    return start <= t < end


# ------------------------------------------------------------
# 메인 스케줄 루프
# ------------------------------------------------------------

# ============================================================
# Scheduler
# ============================================================

def run_scheduler(signals_csv: Path) -> None:
    print("[INFO] YIL Scheduler 시작")
    print(f"[INFO] signals CSV: {signals_csv}")
    print(f"[INFO] 포지션 DB: {DB_PATH}")

    if not signals_csv.exists():
        print(f"[ERROR] CSV 파일을 찾을 수 없습니다: {signals_csv}")
        return

    init_db()

    print("[INFO] KISAPI 초기화 중...")
    kis = KISAPI.from_env()
    print("[INFO] KISAPI 초기화 완료")

    preloaded_signals: Optional[List[dict]] = None
    csv_preload_done = False
    orders_opened = False
    eod_synced = False
    last_check_ts = 0.0

    print("[INFO] 스케줄 루프 진입 (Ctrl+C 로 종료)")

    try:
        while True:
            now = datetime.now().astimezone()
            t = now.time()

            # 1) 08:30~09:00 CSV preload 1회
            if (not csv_preload_done) and between(t, dtime(8, 30), dtime(9, 0)):
                print("[TASK] 08:30~09:00 → CSV 시그널 미리 로딩")
                try:
                    preloaded_signals = load_signals_from_csv(signals_csv)
                    print(f"[INFO] CSV 시그널 미리 로딩 완료: {len(preloaded_signals)}건")
                    csv_preload_done = True
                except Exception as e:
                    print(f"[ERROR] CSV 시그널 로딩 실패: {e}")

            # 2) 09:30 이후 신규 진입 1회
            if (not orders_opened) and t >= dtime(9, 30):
                print("[TASK] 09:30 이후 → 신규 포지션 오픈(주문=DB PENDING)")
                if preloaded_signals is not None:
                    try:
                        adjusted_signals = adjust_signals_based_on_trends(preloaded_signals)

                        for s in adjusted_signals:
                            print(
                                f"종목: {s['name']} ({s['code']}) "
                                f"/ entry={s['entry']} sl={s['sl']} tp={s['tp']} "
                                f"horizon={s.get('horizon')} valid_until={s.get('valid_until')}"
                            )

                        open_new_positions_from_signals(kis, adjusted_signals, now=now)

                    except Exception as e:
                        print(f"[ERROR] 신규 포지션 오픈 중 오류: {e}")

                orders_opened = True

            # 3) 09:31~15:15 장중 동기화 + TP/SL 체크
            if between(t, dtime(9, 31), dtime(15, 15)):
                now_ts = time.time()
                if now_ts - last_check_ts >= CHECK_INTERVAL_SEC:
                    print("[TASK] (장중) PENDING 만료/체결동기화 + TP/SL 체크")
                    try:
                        expire_pending_orders(kis, now)
                        sync_pending_to_open(kis)
                        process_open_positions(kis, do_order=True, now=now)
                    except Exception as e:
                        print(f"[ERROR] 장중 체크 중 오류: {e}")
                    last_check_ts = now_ts

            # 4) 15:15~15:30 강제청산 구간
            if between(t, dtime(15, 15), dtime(15, 30)):
                now_ts = time.time()
                if now_ts - last_check_ts >= CHECK_INTERVAL_SEC:
                    print("[TASK] (15:15~15:30) PENDING 정리 + 체결동기화 + 강제청산(OPEN/EXPIRED)")
                    try:
                        expire_pending_orders(kis, now)
                        sync_pending_to_open(kis)
                        force_close_positions_1515_1530(kis, now=now)
                    except Exception as e:
                        print(f"[ERROR] 강제청산 중 오류: {e}")
                    last_check_ts = now_ts

            # 5) 15:30 이후 EOD sync (주문X): OPEN만 논리정리(여기서는 TP/SL close 없음)
            if (not eod_synced) and t >= dtime(15, 30):
                print("[TASK] 장 마감 이후 → 최종 동기화(do_order=False)")
                try:
                    expire_pending_orders(kis, now)
                    sync_pending_to_open(kis)
                    process_open_positions(kis, do_order=False, now=now)
                except Exception as e:
                    print(f"[ERROR] EOD 동기화 중 오류: {e}")
                eod_synced = True

            # 6) 16:00 이후 종료
            if t >= EOD_EXIT_TIME:
                print("[INFO] EOD_EXIT_TIME 도달 → Scheduler 종료")
                break

            time.sleep(10)

    except KeyboardInterrupt:
        print("\n[INFO] 사용자에 의해 종료(Ctrl+C)")
# ------------------------------------------------------------
# 엔트리포인트
# ------------------------------------------------------------

# 기존 함수/설정 사용한다고 가정
# - run_scheduler(csv_path)
# - last_report_day()
# - config.price_report_dir
# - config.end_date

DAILY_START_TIME = dtime(8, 25)   # 다음날 대기 종료 시각(원하는대로)
AFTER_EOD_SLEEP_SEC = 30          # 16:00 이후 안전 대기(파일 생성 지연 대비)

def seconds_until(target_dt: datetime) -> float:
    now = datetime.now()
    return max(0.0, (target_dt - now).total_seconds())

def next_daily_start_dt(start_time: dtime) -> datetime:
    now = datetime.now()
    today_start = datetime.combine(now.date(), start_time)
    if now < today_start:
        return today_start
    return datetime.combine(now.date() + timedelta(days=1), start_time)

def build_csv_path_for_today() -> Path:
    config.end_date = last_report_day()
    return Path(config.price_report_dir) / f"Auto_Trading_{config.end_date}.csv"


def main(argv: List[str]) -> None:
    print("[INFO] Daily Scheduler Runner 시작(매일 반복 모드)")

    while True:
        try:
            # 1) 오늘(또는 last_report_day 기준) CSV 경로 생성
            csv_path = build_csv_path_for_today()
            print(f"[INFO] 오늘 대상 CSV: {csv_path}")

            # 2) CSV가 아직 생성 안 됐을 수 있으니, 08:30 이전이면 잠깐 기다리거나 재시도
            #    (원하면 더 정교하게: 08:00~08:40 사이에만 재시도 등)
            if not csv_path.exists():
                print(f"[WARN] CSV 없음 → 잠시 후 재시도: {csv_path}")
                time.sleep(60)
                continue

            # 3) 하루치 스케줄 실행 (내부에서 16:00 종료)
            run_scheduler(csv_path)

            # 4) 장 종료 후 파일/DB flush 시간 조금 확보
            time.sleep(AFTER_EOD_SLEEP_SEC)

            # 5) 다음날 시작 시각까지 대기
            start_dt = next_daily_start_dt(DAILY_START_TIME)
            wait_sec = seconds_until(start_dt)
            print(f"[INFO] 다음 실행까지 대기: {start_dt} (약 {int(wait_sec)}초)")
            time.sleep(wait_sec)

        except KeyboardInterrupt:
            print("\n[INFO] 사용자 종료(Ctrl+C)")
            break
        except Exception as e:
            print(f"[ERROR] Daily loop 에러: {e}")
            # 치명 에러라도 다음날 다시 돌 수 있게 잠시 쉬고 계속
            time.sleep(60)

if __name__ == "__main__":
    main(sys.argv)

