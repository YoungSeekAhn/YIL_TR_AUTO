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
from datetime import datetime, time as dtime
from pathlib import Path
from typing import List, Optional
from TRConfig import config
from kis_functions import last_report_day

from kis_trading import (
    KISAPI,
    load_signals_from_csv,
    open_new_positions_from_signals,
    process_open_positions_for_sl,
)
from kis_pos_db import DB_PATH, init_db


# ------------------------------------------------------------
# 설정값
# ------------------------------------------------------------

# SL 체크 주기(초) – 너무 짧으면 트래픽 부담, 너무 길면 손절 지연
SL_CHECK_INTERVAL_SEC = 60  # 1분마다

# 하루 종료 시각 (로컬 시간 기준)
EOD_EXIT_TIME = dtime(16, 0)  # 16:00 이후 종료


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

def run_scheduler(signals_csv: Path) -> None:
    print(f"[INFO] YIL Scheduler 시작")
    print(f"[INFO] signals CSV: {signals_csv}")
    print(f"[INFO] 포지션 DB: {DB_PATH}")

    if not signals_csv.exists():
        print(f"[ERROR] CSV 파일을 찾을 수 없습니다: {signals_csv}")
        return

    # 1) DB 초기화
    init_db()

    # 2) KISAPI 초기화
    print("[INFO] KISAPI 초기화 중...")
    kis = KISAPI.from_env()
    print("[INFO] KISAPI 초기화 완료")

    # 3) 상태 플래그
    preloaded_signals: Optional[List[dict]] = None
    preloaded_time: Optional[datetime] = None

    csv_preload_done = False     # 08:30~전에 한 번만 실행
    orders_opened = False        # 09:00 이후 한 번만 실행
    eod_synced = False           # 장 마감 후 동기화 한 번만 실행

    last_sl_check_ts = 0.0       # SL 체크 마지막 시간 (epoch)

    print("[INFO] 스케줄 루프 진입 (Ctrl+C 로 종료 가능)")

    try:
        while True:
            now = datetime.now()
            t = now.time()

            # -------------------------------
            # 1) 08:30 ~ 08:59 : CSV 미리 로딩
            # -------------------------------
            if (not csv_preload_done) and between(t, dtime(8, 30), dtime(9, 0)):
                print("[TASK] 08:30~09:00 구간 → CSV 시그널 미리 로딩 & 점검")

                try:
                    preloaded_signals = load_signals_from_csv(signals_csv)
                    preloaded_time = now
                    print(f"[INFO] CSV 시그널 미리 로딩 완료: {len(preloaded_signals)}건")
                    csv_preload_done = True
                except Exception as e:
                    print(f"[ERROR] CSV 시그널 로딩 실패: {e}")


            # -------------------------------
            # 2) 09:00 이후: 신규 매수 + TP 예약 (한 번만)
            # -------------------------------
            if (not orders_opened) and t >= dtime(9, 0):
                print("[TASK] 09:00 이후 → 신규 포지션 오픈 (매수 + TP 예약)")

                # 만약 08:30 전에 CSV 미리 못 읽었으면 이 시점에라도 로딩
                if preloaded_signals is None:
                    try:
                        preloaded_signals = load_signals_from_csv(signals_csv)
                        preloaded_time = now
                        print(f"[INFO] (지연 로딩) CSV 시그널 로딩: {len(preloaded_signals)}건")
                    except Exception as e:
                        print(f"[ERROR] 09:00 시점 CSV 로딩 실패 → 신규 매수 스킵: {e}")
                        orders_opened = True  # 더 이상 시도하지 않음
                    else:
                        # 로딩 성공한 경우에만 주문 시도
                        pass

                # 시그널이 로딩되어 있으면 매수/TP예약 실행
                if preloaded_signals is not None:
                    try:
                        open_new_positions_from_signals(kis, preloaded_signals)
                    except Exception as e:
                        print(f"[ERROR] 신규 포지션 오픈 중 오류: {e}")

                orders_opened = True  # 한 번만 실행

            # -------------------------------
            # 3) 09:00 ~ 15:15 : SL 주기적 체크
            # -------------------------------
            if between(t, dtime(9, 0), dtime(15, 15)):
                now_ts = time.time()
                if now_ts - last_sl_check_ts >= SL_CHECK_INTERVAL_SEC:
                    print("[TASK] SL 체크 실행 (장중)")
                    try:
                        process_open_positions_for_sl(kis)
                    except Exception as e:
                        print(f"[ERROR] SL 체크 중 오류: {e}")
                    last_sl_check_ts = now_ts

            # -------------------------------
            # 4) 15:15 ~ 15:30 : 신규 매수 금지, SL만 계속
            #    (현재 구현에서 신규 매수는 09:00에 한 번만 이라 별도 제어는 없음)
            #    여기서는 SL 체크만 계속 유지
            # -------------------------------
            if between(t, dtime(15, 15), dtime(15, 30)):
                now_ts = time.time()
                if now_ts - last_sl_check_ts >= SL_CHECK_INTERVAL_SEC:
                    print("[TASK] SL 체크 실행 (마감 전 15분 구간)")
                    try:
                        process_open_positions_for_sl(kis)
                    except Exception as e:
                        print(f"[ERROR] SL 체크 중 오류: {e}")
                    last_sl_check_ts = now_ts

            # -------------------------------
            # 5) 15:30 이후: 장 마감 후 동기화 1회
            # -------------------------------
            if (not eod_synced) and t >= dtime(15, 30):
                print("[TASK] 장 마감 이후 → 최종 SL/TP_SYNC 동기화")
                try:
                    process_open_positions_for_sl(kis)
                except Exception as e:
                    print(f"[ERROR] 장 마감 후 SL/TP_SYNC 동기화 중 오류: {e}")
                eod_synced = True

            # -------------------------------
            # 6) 16:00 이후: 프로그램 종료
            # -------------------------------
            if t >= EOD_EXIT_TIME:
                print("[INFO] EOD_EXIT_TIME 도달 → YIL Scheduler 종료")
                break

            # -------------------------------
            # 7) 다음 루프까지 잠시 대기
            # -------------------------------
            time.sleep(10)  # 10초마다 루프 (SL 체크 간격은 별도로 제어)

    except KeyboardInterrupt:
        print("\n[INFO] 사용자에 의해 종료(Ctrl+C)")


# ------------------------------------------------------------
# 엔트리포인트
# ------------------------------------------------------------

def main(argv: List[str]) -> None:
    if len(argv) < 2:
        print("Usage: python yil_scheduler.py <signals_csv_path>")
        print("예:   python yil_scheduler.py signals_2025-12-02.csv")
        return
        # 리포트 일자/경로
    config.end_date = last_report_day()
    csv_path = Path(config.price_report_dir) / f"Report_{config.end_date}" / f"Trading_price_{config.end_date}.csv"
    #csv_path = Path('C:/Users/30211/vs_code/YIL_TR_AUTO/Report_20251128/Trading_price_20251128.csv')
    
    if not csv_path.exists():
        print(f"[ERROR] CSV 파일을 찾을 수 없습니다: {csv_path}")
        return
    run_scheduler(csv_path)


if __name__ == "__main__":
    main(sys.argv)
