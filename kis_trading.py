"""
kis_trading.py
- 일별 CSV 시그널을 읽어서 신규 포지션 오픈
- DB(kis_pos_db)에 기록된 OPEN 포지션을 TP/SL/만기 기준으로 청산
- KIS API (kis_functions) + SQLite 포지션 DB (kis_pos_db) 연동
"""

from __future__ import annotations

import csv
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from kis_functions import KISAPI
from kis_pos_db import (
    DB_PATH,
    Position,
    init_db,
    insert_position,
    get_open_positions,
    close_position,
)


# ============================================================
# 유틸 헬퍼
# ============================================================

def float_or_none(v: Any) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    try:
        return float(s)
    except Exception:
        return None


def to_iso_now() -> str:
    """현재 시각을 ISO8601 문자열로 반환 (타임존 포함)"""
    return datetime.now().astimezone().isoformat()


def parse_iso(s: str) -> Optional[datetime]:
    """ISO8601 문자열 → datetime (실패하면 None)"""
    if not s:
        return None
    try:
        # 'Z' → '+00:00' 보정
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# ============================================================
# CSV 시그널 로딩
# ============================================================

def load_signals_from_csv(csv_path: Path) -> List[Dict[str, Any]]:
    """
    일별 시그널 CSV를 읽어서 dict 리스트로 반환.

    기대하는 컬럼명 (질문에서 준 헤더 기준):
    - 종목명
    - 종목코드
    - 권장호라이즌
    - 매수가(entry)
    - 익절가(tp)
    - 손절가(sl)
    - RR
    - Score_1w
    - ord_qty
    - side
    - confidence
    - valid_until
    """
    signals: List[Dict[str, Any]] = []

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                code_raw = (row.get("종목코드") or "").strip()
                if not code_raw:
                    continue
                # 6자리 코드로 맞추기 (앞에 0 padding)
                code = code_raw.zfill(6)

                name = (row.get("종목명") or "").strip()

                side = (row.get("side") or "BUY").upper()
                qty = int(float(row.get("ord_qty") or 0))

                entry = float(row.get("매수가(entry)") or 0)
                tp = float_or_none(row.get("익절가(tp)"))
                sl = float_or_none(row.get("손절가(sl)"))

                score_1w = float_or_none(row.get("Score_1w"))
                rr = float_or_none(row.get("RR"))
                confidence = float_or_none(row.get("confidence"))
                horizon = (row.get("권장호라이즌") or "").strip()
                valid_until = (row.get("valid_until") or "").strip()

                if qty <= 0 or entry <= 0:
                    # 수량이나 매수가가 0 또는 음수면 스킵
                    continue

                signals.append(
                    {
                        "code": code,
                        "name": name,
                        "side": side,
                        "qty": qty,
                        "entry": entry,
                        "tp": tp,
                        "sl": sl,
                        "score_1w": score_1w,
                        "rr": rr,
                        "confidence": confidence,
                        "horizon": horizon,
                        "valid_until": valid_until,
                        "raw_row": row,
                    }
                )
            except Exception as e:
                print(f"[WARN] CSV row 파싱 실패: {e}, row={row}")
                continue

    return signals


# ============================================================
# 신규 시그널 → 포지션 오픈
# ============================================================

def open_new_positions_from_signals(
    kis: KISAPI,
    signals: List[Dict[str, Any]],
) -> None:
    """
    1) 계좌 예수금 확인
    2) KIS 현재 보유종목 + DB OPEN 포지션 확인
    3) CSV 시그널 순서대로:
       - 이미 계좌 보유 or DB OPEN이면 스킵
       - entry * qty > 남은 예수금이면 스킵
       - 지정가 매수 주문 (buy_limit)
       - DB에 Position(status="OPEN") 기록
    """
    if not signals:
        print("[INFO] CSV 시그널 없음 → 신규 포지션 오픈 스킵")
        return

    # 계좌 요약 / 보유 종목
    summary = kis.account.get_summary()
    cash = summary.get("cash", 0.0)
    print(f"[INFO] 현재 예수금 (dnca_tot_amt 기준): {cash:,.0f}원")

    kis_pos_map = kis.account.get_positions_map()
    print(f"[INFO] KIS 현재 보유종목 수: {len(kis_pos_map)}")

    open_db_positions = get_open_positions()
    open_db_codes = {p.code for p in open_db_positions}
    print(f"[INFO] DB OPEN 포지션 수: {len(open_db_positions)}")

    remaining_cash = float(cash)

    for sig in signals:
        code = sig["code"]
        name = sig["name"]
        side = sig["side"]
        qty = sig["qty"]
        entry = sig["entry"]
        tp = sig["tp"]
        sl = sig["sl"]

        if side != "BUY":
            print(f"[SKIP] {code} {name}: side={side} (현재는 BUY만 처리)")
            continue

        # 1) 이미 KIS 계좌에 보유한 종목이면 스킵
        if code in kis_pos_map:
            print(f"[SKIP] {code} {name}: 계좌에 이미 보유 중 → 추가 매수 금지")
            continue

        # 2) DB에 OPEN 포지션이 있으면 스킵
        if code in open_db_codes:
            print(f"[SKIP] {code} {name}: DB에 OPEN 포지션 존재 → 추가 매수 금지")
            continue

        est_cost = entry * qty
        if est_cost > remaining_cash:
            print(
                f"[SKIP] {code} {name}: 예상 매수금액 {est_cost:,.0f}원이 남은 예수금 {remaining_cash:,.0f}원을 초과"
            )
            continue

        # 3) 지정가 매수 주문 (ORD_DVSN=00, 지정가)
        try:
            price_int = int(round(entry))
            print(
                f"[ORDER] BUY LIMIT {code} {name} qty={qty} price={price_int} "
                f"(예상금액 {est_cost:,.0f}원)"
            )
            resp = kis.order.buy_limit(code, qty, price_int)
            print("  → 주문 응답:", resp.get("msg1", resp))
        except Exception as e:
            print(f"[ERROR] {code} {name} 매수 주문 실패: {e}")
            continue

        # 남은 예수금 감소(추정치)
        remaining_cash -= est_cost

        # 4) DB에 포지션 기록 (status="OPEN")
        now_iso = to_iso_now()
        pos = Position(
            id=None,
            code=code,
            name=name,
            side=side,
            qty=qty,
            entry=entry,
            tp=tp,
            sl=sl,
            open_time=now_iso,
            close_time=None,
            status="OPEN",
            exit_price=None,
            exit_reason=None,
            score_1w=sig["score_1w"],
            rr=sig["rr"],
            confidence=sig["confidence"],
            horizon=sig["horizon"],
            valid_until=sig["valid_until"],
            note="from daily CSV signal",
        )

        new_id = insert_position(pos)
        print(f"  → DB 포지션 기록 완료 (id={new_id})")


# ============================================================
# OPEN 포지션 TP/SL/만기 체크 및 청산
# ============================================================

def process_open_positions(kis: KISAPI) -> None:
    """
    DB에 OPEN 상태인 포지션들을 순회하면서:
    - side=BUY 기준:
        - 현재가 >= tp → TP 청산
        - 현재가 <= sl → SL 청산
    - valid_until 이 지난 경우 → TIMEOUT 청산
    """
    open_positions = get_open_positions()
    if not open_positions:
        print("[INFO] DB에 OPEN 포지션 없음 → 청산 로직 스킵")
        return

    print(f"[INFO] OPEN 포지션 {len(open_positions)}건 TP/SL/만기 체크 시작")

    now = datetime.now().astimezone()

    for pos in open_positions:
        # 현재가 조회
        try:
            q = kis.market.get_quote(pos.code)
            out = q.get("output", {})
            cur_price = float(out.get("stck_prpr", "0") or 0.0)
        except Exception as e:
            print(f"[WARN] {pos.code} {pos.name}: 현재가 조회 실패: {e}")
            continue

        if cur_price <= 0:
            print(f"[WARN] {pos.code} {pos.name}: 현재가 0 또는 조회 실패로 청산 판단 스킵")
            continue

        print(
            f"[CHECK] {pos.code} {pos.name}: entry={pos.entry}, "
            f"tp={pos.tp}, sl={pos.sl}, cur={cur_price}"
        )

        reason: Optional[str] = None

        # 1) TP/SL 조건
        if pos.side.upper() == "BUY":
            if pos.tp is not None and cur_price >= pos.tp:
                reason = "TP"
            elif pos.sl is not None and cur_price <= pos.sl:
                reason = "SL"
        else:
            # SELL(숏) 전략을 나중에 도입한다면 여기서 반대로 처리
            pass

        # 2) 만기(valid_until) 체크 (TP/SL 안 걸렸을 때만)
        if reason is None and pos.valid_until:
            dt_valid = parse_iso(pos.valid_until)
            if dt_valid is not None and now >= dt_valid:
                reason = "TIMEOUT"

        if reason is None:
            continue  # 아직 청산 조건 미충족

        # ---- 청산 처리 ----
        try:
            qty = pos.qty
            print(f"[EXIT] {pos.code} {pos.name}: reason={reason}, qty={qty}, px={cur_price}")

            # 시장가 매도
            if pos.side.upper() == "BUY":
                resp = kis.order.sell_market(pos.code, qty)
            else:
                # 나중에 숏 로직이 생기면 여기서 BUY로 청산
                resp = kis.order.buy_market(pos.code, qty)
            print("  → 주문 응답:", resp.get("msg1", resp))
        except Exception as e:
            print(f"[ERROR] {pos.code} {pos.name} 청산 주문 실패: {e}")
            continue

        # DB 포지션 상태 업데이트 (실현손익, 수익률, 보유일수 계산 포함)
        exit_time_iso = now.isoformat()
        try:
            close_position(
                pos_id=pos.id,
                exit_price=cur_price,
                exit_time=exit_time_iso,
                exit_reason=reason,
            )
            print(f"  → DB 포지션 id={pos.id} CLOSED (reason={reason})")
        except Exception as e:
            print(f"[ERROR] DB close_position 실패 (id={pos.id}): {e}")


# ============================================================
# main
# ============================================================

def main(argv: List[str]) -> None:
    """
    사용법:
        python kis_trading.py signals_2025-12-01.csv

    동작:
      1) DB 초기화 (없으면 생성)
      2) KISAPI.from_env() 로 접속 준비
      3) CSV 시그널 읽어서 신규 포지션 오픈
      4) DB의 OPEN 포지션 TP/SL/만기 체크 후 청산
    """

        # 리포트 일자/경로
    #config.end_date = last_report_day()
    #csv_path = Path(config.price_report_dir) / f"Report_{config.end_date}" / f"Trading_price_{config.end_date}.csv"
    csv_path = Path('C:/Users/30211/vs_code/YIL_TR_AUTO/Report_20251128/Trading_price_20251128.csv')
    
    if not csv_path.exists():
        print(f"[ERROR] CSV 파일을 찾을 수 없습니다: {csv_path}")
        return

    print(f"[INFO] 포지션 DB 경로: {DB_PATH}")
    init_db()

    print("[INFO] KISAPI 초기화")
    kis = KISAPI.from_env()

    # 1) CSV 시그널 로딩
    signals = load_signals_from_csv(csv_path)
    print(f"[INFO] CSV 시그널 {len(signals)}건 로딩 완료")

    # 2) 신규 포지션 오픈
    open_new_positions_from_signals(kis, signals)

    # 3) OPEN 포지션 TP/SL/만기 체크 및 청산
    process_open_positions(kis)


if __name__ == "__main__":
    main(sys.argv)
