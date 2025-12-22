
"""
kis_trading.py (실거래용 최종 정리본)

정책(요구사항 반영)
1) 09:30 이후 신규 진입은 1회만 수행
   - 주문 발생 시 DB status = "PENDING"
   - 체결(계좌 보유에 잡힘) 확인 시 DB status = "OPEN" 전환

2) valid_until(=horizon 기반 신호 유효종료 시각, 보통 15:15 KST)
   - OPEN 포지션: valid_until 도달 시 장중 청산하지 않음
     -> DB status = "EXPIRED"로 전환(재진입 금지 목적)
     -> 실제 청산은 15:15~15:30 강제청산 구간에서만 수행
   - PENDING(미체결 주문): valid_until 도달 시 '청산'이 아니라 '주문 취소'가 맞음
     -> 취소 시도 후 DB status = "CANCELLED"로 전환

3) 15:15~15:30 강제청산
   - 대상: DB status in ("OPEN", "EXPIRED")
   - 목표: TP/2 ~ SL/2 밴드 내에서 "최상 가격(best bid)" 기반으로 제한가 청산 시도
   - 마감 임박에는 시장가로 fallback

필수 전제
- kis_pos_db.py에 아래 함수가 추가되어 있어야 함:
  - get_positions_by_status(statuses: List[str]) -> List[Position]
  - get_codes_by_status(statuses: List[str]) -> List[str]
  - update_position_fields(pos_id: int, fields: Dict[str,Any]) -> bool

주의
- KIS API 메서드명은 프로젝트마다 다를 수 있음:
  - buy_limit, sell_market, sell_limit, cancel/cancel_order 등
  - 이 파일은 "있으면 사용하고 없으면 안전하게 fallback"하도록 작성됨
"""

from __future__ import annotations

import csv
import re
import sys
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from TRConfig import config
from kis_tr_adj import adjust_signals_based_on_trends
from kis_functions import KISAPI, last_report_day
from kis_pos_db import (
    DB_PATH,
    Position,
    init_db,
    insert_position,
    get_open_positions,
    close_position,
    get_positions_by_status,
    get_codes_by_status,
    update_position_fields,
)

# ============================================================
# Utils
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
    return datetime.now().astimezone().isoformat()


def between(t: dtime, start: dtime, end: dtime) -> bool:
    return start <= t < end


def parse_iso_aware(s: str, default_tz) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt


def is_kis_order_ok(resp: Any) -> bool:
    """
    KIS 응답이 dict이고 rt_cd=0이면 성공으로 간주.
    환경별로 msg1만 오는 경우도 있어 보수적으로 처리.
    """
    if not isinstance(resp, dict):
        return False
    rt_cd = str(resp.get("rt_cd", ""))
    if rt_cd == "0":
        return True
    msg1 = str(resp.get("msg1", "") or "")
    if "정상" in msg1 or "success" in msg1.lower():
        return True
    return False


def safe_str(v: Any) -> str:
    try:
        return str(v)
    except Exception:
        return ""


# ============================================================
# Tick alignment (KRX)
# ============================================================

def krx_tick_size(price: float) -> int:
    p = float(price)
    if p < 1000:
        return 1
    if p < 5000:
        return 5
    if p < 10000:
        return 10
    if p < 50000:
        return 50
    if p < 100000:
        return 100
    if p < 500000:
        return 500
    return 1000


def align_price_to_tick(price: float, side: str) -> int:
    """
    BUY: 내림(더 싸게) / SELL: 올림(더 비싸게) 정렬
    """
    p = float(price)
    tick = krx_tick_size(p)
    if tick <= 0:
        return int(round(p))

    if side.upper() == "BUY":
        aligned = (int(p) // tick) * tick
    else:
        aligned = ((int(p) + tick - 1) // tick) * tick

    if aligned <= 0:
        aligned = tick
    return int(aligned)


# ============================================================
# valid_until 계산 (CSV에 없을 때만 fallback으로 사용)
# - horizon_days=1 -> 당일 15:15
# - horizon_days=2 -> 다음 거래일(주말 제외) 15:15
# ============================================================

def add_trading_days_weekend_only(d: datetime, n: int) -> datetime:
    cur = d
    added = 0
    while added < n:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            added += 1
    return cur


def compute_valid_until_fallback(now: datetime, horizon_days: int) -> str:
    if now.tzinfo is None:
        now = now.astimezone()
    target = add_trading_days_weekend_only(now, max(horizon_days - 1, 0))
    valid = target.replace(hour=15, minute=15, second=0, microsecond=0)
    return valid.isoformat()


def parse_horizon_days(h: Optional[str]) -> Optional[int]:
    """
    horizon 예: "1", "2", "h1", "d2", "2d", "day2" 등
    숫자만 뽑아 일수로 해석(정교 규칙이 있다면 여기 확장).
    """
    if not h:
        return None
    m = re.search(r"(\d+)", str(h))
    if not m:
        return None
    try:
        v = int(m.group(1))
        return v if v > 0 else None
    except Exception:
        return None


# ============================================================
# CSV signals
# ============================================================

def load_signals_from_csv(csv_path: Path) -> List[Dict[str, Any]]:
    signals: List[Dict[str, Any]] = []

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                code_raw = (row.get("종목코드") or "").strip()
                if not code_raw:
                    continue
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
# Order-id extraction (stored in note)
# ============================================================

def extract_order_id(note: Optional[str]) -> Optional[str]:
    if not note:
        return None
    m = re.search(r"order_id=([A-Za-z0-9\-_]+)", note)
    return m.group(1) if m else None


def build_note_with_order_id(base: str, resp: Any) -> str:
    note = base
    if isinstance(resp, dict):
        order_tag = None
        for k in ("odno", "ODNO", "order_no", "orderNo"):
            if resp.get(k):
                order_tag = safe_str(resp.get(k))
                break
        if order_tag:
            note += f" | order_id={order_tag}"
    return note


# ============================================================
# 1) Open new positions: 주문=DB PENDING
# ============================================================

def open_new_positions_from_signals(kis: KISAPI, signals: List[Dict[str, Any]], now: Optional[datetime] = None) -> None:
    """
    09:30 이후 1회 실행용.
    - 주문 성공 시 DB에 status="PENDING"으로 기록
    - 체결 동기화는 sync_pending_to_open에서 수행
    """
    if not signals:
        print("[INFO] CSV 시그널 없음 → 신규 포지션 오픈 스킵")
        return

    if now is None:
        now = datetime.now().astimezone()

    summary = kis.account.get_summary()
    cash = float(summary.get("cash", 0.0) or 0.0)
    cash_d2 = float(summary.get("d2_amt", 0.0) or 0.0)
    
    print(f"[INFO] 현재 예수금(dnca_tot_amt 기준): {cash:,.0f}원")
    print(f"[INFO] D2 예치금(d2_amt 기준): {cash_d2:,.0f}원")

    kis_pos_map = kis.account.get_positions_map()
    print(f"[INFO] KIS 현재 보유종목 수: {len(kis_pos_map)}")

    # 재진입 금지: OPEN/EXPIRED/PENDING/CLOSING/CANCELLED 포함 권장
    blocked_codes = set(get_codes_by_status(["OPEN", "EXPIRED", "PENDING", "CLOSING"]))
    print(f"[INFO] DB 재진입 금지 코드 수(OPEN/EXPIRED/PENDING/CLOSING): {len(blocked_codes)}")

    remaining_cash = cash

    for sig in signals:
        code = sig["code"]
        name = sig["name"]
        side = (sig.get("side") or "BUY").upper()
        qty = int(sig["qty"])
        entry = float(sig["entry"])
        tp = sig.get("tp")
        sl = sig.get("sl")
        horizon = sig.get("horizon")
        valid_until = (sig.get("valid_until") or "").strip()

        if side != "BUY":
            print(f"[SKIP] {code} {name}: side={side} (현재는 BUY만 처리)")
            continue

        if code in kis_pos_map:
            print(f"[SKIP] {code} {name}: 계좌에 이미 보유 중 → 추가 매수 금지")
            continue

        if code in blocked_codes:
            print(f"[SKIP] {code} {name}: DB에 OPEN/EXPIRED/PENDING/CLOSING 존재 → 재진입 금지")
            continue

        est_cost = entry * qty
        if est_cost > remaining_cash:
            print(f"[SKIP] {code} {name}: 예상 매수금액 {est_cost:,.0f}원이 남은 예수금 {remaining_cash:,.0f}원을 초과")
            continue

        # valid_until이 CSV에 없으면 horizon 기반 fallback 계산(선택)
        if not valid_until:
            hdays = parse_horizon_days(horizon)
            if hdays is not None:
                valid_until = compute_valid_until_fallback(now, hdays)

        # 지정가 매수
        try:
            #price_int = align_price_to_tick(entry, side="BUY")
            price_int = entry # 수정: 원가 기준으로 주문
            print(f"[ORDER] BUY LIMIT {code} {name} qty={qty} price={price_int} (예상금액 {est_cost:,.0f}원)")
            resp = kis.order.buy_limit(code, qty, price_int)
            print("  → 주문 응답:", (resp.get("msg1") if isinstance(resp, dict) else resp))

            if not is_kis_order_ok(resp):
                print(f"[ERROR] {code} {name}: 주문 실패로 판단 → DB 기록 생략")
                continue
        except Exception as e:
            print(f"[ERROR] {code} {name} 매수 주문 실패: {e}")
            continue

        remaining_cash -= est_cost

        # DB 기록: PENDING
        note = build_note_with_order_id("from daily CSV signal", resp)

        pos = Position(
            id=None,
            code=code,
            name=name,
            side=side,
            qty=qty,
            entry=entry,
            tp=tp,
            sl=sl,
            open_time=now.isoformat(),
            close_time=None,
            status="PENDING",
            exit_price=None,
            exit_reason=None,
            score_1w=sig.get("score_1w"),
            rr=sig.get("rr"),
            confidence=sig.get("confidence"),
            horizon=horizon,
            valid_until=valid_until if valid_until else None,
            note=note,
        )

        new_id = insert_position(pos)
        print(f"  → DB 포지션 기록 완료 (id={new_id}, status=PENDING)")


# ============================================================
# 2) Sync: PENDING -> OPEN (체결 확인)
# ============================================================

def sync_pending_to_open(kis: KISAPI) -> None:
    """
    DB PENDING 포지션을 조회해서 계좌 보유현황에 종목이 잡히면 체결 완료로 간주 → OPEN 전환.
    (정교하게 하려면 체결조회 API를 붙이는 것이 이상적)
    """
    pending = get_positions_by_status(["PENDING"])
    if not pending:
        return

    try:
        pos_map = kis.account.get_positions_map()
    except Exception as e:
        print(f"[WARN] sync_pending_to_open: 계좌 보유 조회 실패: {e}")
        return

    for p in pending:
        if p.code not in pos_map:
            continue

        info = pos_map.get(p.code, {}) or {}
        new_fields: Dict[str, Any] = {"status": "OPEN"}

        # 환경별 키 차이 대응(있으면 보정)
        for qty_key in ("qty", "hldg_qty", "hold_qty", "hldg_qty2"):
            if qty_key in info and info.get(qty_key) is not None:
                try:
                    new_fields["qty"] = int(float(info[qty_key]))
                except Exception:
                    pass
                break

        for px_key in ("avg_price", "pchs_avg_pric", "avg_prpr", "pchs_pric"):
            if px_key in info and info.get(px_key) is not None:
                try:
                    new_fields["entry"] = float(info[px_key])
                except Exception:
                    pass
                break

        ok = update_position_fields(p.id, new_fields)
        print(f"[SYNC] {p.code} {p.name}: PENDING → OPEN ({'OK' if ok else 'FAIL'})")


# ============================================================
# 3) Expire: PENDING valid_until 도달 -> 주문취소 시도 -> CANCELLED
# ============================================================

def expire_pending_orders(kis: KISAPI, now: datetime) -> None:
    """
    PENDING 주문 중 valid_until이 지난 건:
    - 주문 취소 시도(가능하면)
    - DB status를 CANCELLED로 변경
    """
    pending = get_positions_by_status(["PENDING"])
    if not pending:
        return

    if now.tzinfo is None:
        now = now.astimezone()

    for p in pending:
        if not p.valid_until:
            continue

        dt_valid = parse_iso_aware(p.valid_until, default_tz=now.tzinfo)
        if dt_valid is None:
            continue

        if now < dt_valid:
            continue

        order_id = extract_order_id(p.note)
        cancelled_ok = False

        # 주문 취소 API는 프로젝트마다 다르므로 "있으면" 호출
        try:
            if order_id and hasattr(kis.order, "cancel"):
                resp = kis.order.cancel(order_id=order_id)
                cancelled_ok = is_kis_order_ok(resp)
            elif order_id and hasattr(kis.order, "cancel_order"):
                resp = kis.order.cancel_order(order_id)
                cancelled_ok = is_kis_order_ok(resp)
            else:
                # 취소 API가 없거나 order_id를 못 뽑으면, 일단 DB만 만료 처리(안전상 재진입 금지 목적)
                cancelled_ok = True
        except Exception as e:
            print(f"[WARN] PENDING 취소 실패: {p.code} {p.name} order_id={order_id}, err={e}")

        if cancelled_ok:
            new_note = (p.note or "")
            if "expired_pending" not in new_note:
                new_note = (new_note + " | expired_pending").strip(" |")
            ok = update_position_fields(p.id, {"status": "CANCELLED", "note": new_note})
            print(f"[PENDING-EXPIRE] {p.code} {p.name}: PENDING→CANCELLED ({'OK' if ok else 'FAIL'})")


# ============================================================
# 4) Intraday: TP/SL + OPEN valid_until -> EXPIRED
# ============================================================

_EXIT_INFLIGHT: Set[int] = set()


def process_open_positions(kis: KISAPI, do_order: bool = True, now: Optional[datetime] = None) -> None:
    """
    장중 체크
    - OPEN 대상만 TP/SL 체크 후 즉시 청산
    - OPEN의 valid_until 도달 시: 장중 청산 X, status="EXPIRED"만 수행
    """
    open_positions = get_open_positions()
    if not open_positions:
        print("[INFO] DB에 OPEN 포지션 없음 → 장중 체크 스킵")
        return

    if now is None:
        now = datetime.now().astimezone()

    print(f"[INFO] OPEN 포지션 {len(open_positions)}건 TP/SL/만기 체크 (do_order={do_order})")

    # 실거래 안전: 계좌 보유현황 체크
    kis_pos_map: Dict[str, Any] = {}
    try:
        kis_pos_map = kis.account.get_positions_map()
    except Exception as e:
        print(f"[WARN] 계좌 보유 조회 실패(매도 안전체크 약화): {e}")

    for pos in open_positions:
        if pos.id in _EXIT_INFLIGHT:
            continue
        _EXIT_INFLIGHT.add(pos.id)

        try:
            # 현재가
            try:
                q = kis.market.get_quote(pos.code)
                out = q.get("output", {}) if isinstance(q, dict) else {}
                cur_price = float(out.get("stck_prpr", "0") or 0.0)
            except Exception as e:
                print(f"[WARN] {pos.code} {pos.name}: 현재가 조회 실패: {e}")
                continue

            if cur_price <= 0:
                print(f"[WARN] {pos.code} {pos.name}: 현재가 0 → 스킵")
                continue

            print(f"[CHECK] {pos.code} {pos.name}: entry={pos.entry}, tp={pos.tp}, sl={pos.sl}, cur={cur_price}")

            # 1) TP/SL
            reason: Optional[str] = None
            if (pos.side or "").upper() == "BUY":
                if pos.tp is not None and cur_price >= float(pos.tp):
                    reason = "TP"
                elif pos.sl is not None and cur_price <= float(pos.sl):
                    reason = "SL"

            # 2) OPEN TIMEOUT(valid_until): 장중 청산 X, EXPIRED로 전환만
            if reason is None and pos.valid_until:
                dt_valid = parse_iso_aware(pos.valid_until, default_tz=now.tzinfo)
                if dt_valid is not None and now >= dt_valid:
                    ok = update_position_fields(pos.id, {"status": "EXPIRED"})
                    print(f"[EXPIRE] {pos.code} {pos.name}: valid_until 지남 → status=EXPIRED ({'OK' if ok else 'FAIL'})")
                    continue

            if reason is None:
                continue

            print(f"[HIT] {pos.code} {pos.name}: reason={reason}, qty={pos.qty}, cur={cur_price}, do_order={do_order}")

            # 실거래 안전: 계좌 보유 없으면 매도 금지
            if do_order and (pos.code not in kis_pos_map):
                print(f"[SAFE-SKIP] {pos.code} {pos.name}: 계좌 보유에 없음 → 매도 스킵")
                continue

            order_ok = True
            if do_order:
                try:
                    qty = int(pos.qty)
                    if (pos.side or "").upper() == "BUY":
                        resp = kis.order.sell_market(pos.code, qty)
                    else:
                        resp = kis.order.buy_market(pos.code, qty)
                    print("  → 주문 응답:", (resp.get("msg1") if isinstance(resp, dict) else resp))
                    order_ok = is_kis_order_ok(resp)
                except Exception as e:
                    order_ok = False
                    print(f"[ERROR] {pos.code} {pos.name}: 청산 주문 실패: {e}")

            if (not do_order) or (do_order and order_ok):
                try:
                    close_position(
                        pos_id=pos.id,
                        exit_price=cur_price,
                        exit_time=now.isoformat(),
                        exit_reason=reason,
                    )
                    print(f"  → DB 포지션 id={pos.id} CLOSED (reason={reason})")
                except Exception as e:
                    print(f"[ERROR] DB close_position 실패 (id={pos.id}): {e}")
            else:
                print(f"[INFO] 주문 실패로 DB CLOSE 보류 (id={pos.id})")

        finally:
            _EXIT_INFLIGHT.discard(pos.id)


# ============================================================
# 5) Force close 15:15~15:30 (OPEN + EXPIRED)
# ============================================================

def calc_force_band(entry: float, tp: Optional[float], sl: Optional[float]) -> Optional[Tuple[float, float]]:
    if tp is None or sl is None:
        return None
    mid_tp = entry + (tp - entry) * 0.5
    mid_sl = entry - (entry - sl) * 0.5
    lo, hi = (mid_sl, mid_tp) if mid_sl <= mid_tp else (mid_tp, mid_sl)
    return (lo, hi)


def get_best_bid(kis: KISAPI, code: str) -> Optional[float]:
    """
    orderbook이 있으면 best bid 우선, 없으면 quote에서 후보키 시도.
    """
    try:
        if hasattr(kis.market, "get_orderbook"):
            ob = kis.market.get_orderbook(code)
            out = ob.get("output", {}) if isinstance(ob, dict) else {}
            for k in ("bidp1", "stck_bidp1", "best_bid"):
                v = out.get(k)
                if v:
                    return float(v)
    except Exception:
        pass

    try:
        q = kis.market.get_quote(code)
        out = q.get("output", {}) if isinstance(q, dict) else {}
        for k in ("stck_bidp", "bidp", "bid_prpr"):
            v = out.get(k)
            if v:
                return float(v)
    except Exception:
        pass

    return None


def force_close_positions_1515_1530(
    kis: KISAPI,
    now: datetime,
    hard_deadline: dtime = dtime(15, 29, 30),
    market_deadline: dtime = dtime(15, 29, 50),
) -> None:
    """
    강제청산 대상: status in ('OPEN', 'EXPIRED')
    - t < hard_deadline: 밴드 밖이면 HOLD(재시도)
    - t >= hard_deadline: 밴드 하한 아래면 하한가로 제한가 시도
    - t >= market_deadline: 시장가로 무조건 청산
    """
    positions = get_positions_by_status(["OPEN", "EXPIRED"])
    if not positions:
        print("[INFO] OPEN/EXPIRED 포지션 없음 → 강제청산 스킵")
        return

    if now.tzinfo is None:
        now = now.astimezone()

    t = now.time()

    # 계좌 보유 체크
    kis_pos_map: Dict[str, Any] = {}
    try:
        kis_pos_map = kis.account.get_positions_map()
    except Exception as e:
        print(f"[WARN] 강제청산: 계좌 보유 조회 실패: {e}")

    # EXPIRED 우선
    positions.sort(key=lambda p: (0 if p.status == "EXPIRED" else 1, p.open_time))

    print(f"[FORCE] 강제청산 시작: {len(positions)}건 (now={now.isoformat()})")

    for pos in positions:
        if (pos.side or "").upper() != "BUY":
            continue
        if pos.qty <= 0:
            continue
        if pos.id in _EXIT_INFLIGHT:
            continue
        if pos.code not in kis_pos_map:
            print(f"[SAFE-SKIP] {pos.code} {pos.name}: 계좌 보유에 없음 → 강제청산 스킵")
            continue

        _EXIT_INFLIGHT.add(pos.id)
        try:
            best_bid = get_best_bid(kis, pos.code)

            # 시장가 fallback
            if t >= market_deadline:
                print(f"[FORCE-MKT] {pos.code} {pos.name}: 마감 임박 → 시장가 청산")
                try:
                    resp = kis.order.sell_market(pos.code, int(pos.qty))
                    print("  → 주문 응답:", (resp.get("msg1") if isinstance(resp, dict) else resp))
                    if is_kis_order_ok(resp):
                        close_position(pos.id, exit_price=float(best_bid or 0.0), exit_time=now.isoformat(), exit_reason="FORCE_MKT")
                    else:
                        print(f"[WARN] {pos.code} {pos.name}: 시장가 주문 실패/불확실")
                except Exception as e:
                    print(f"[ERROR] {pos.code} {pos.name}: FORCE 시장가 실패: {e}")
                continue

            if best_bid is None or best_bid <= 0:
                print(f"[HOLD] {pos.code} {pos.name}: best_bid 조회 실패 → 재시도")
                continue

            band = calc_force_band(float(pos.entry), float_or_none(pos.tp), float_or_none(pos.sl))
            limit_px = best_bid

            if band is not None:
                band_lo, band_hi = band

                if t < hard_deadline:
                    # strict: 밴드 내에서만 청산 시도
                    if not (band_lo <= best_bid <= band_hi):
                        print(f"[HOLD] {pos.code} {pos.name}: best_bid={best_bid} 밴드({band_lo}~{band_hi}) 밖 → 재시도")
                        continue
                else:
                    # relax: 밴드 하한 아래면 하한으로 제한가
                    if best_bid < band_lo:
                        limit_px = band_lo

            limit_px_int = align_price_to_tick(limit_px, side="SELL")
            print(f"[FORCE-LMT] {pos.code} {pos.name}: qty={pos.qty}, best_bid={best_bid}, limit_px={limit_px_int}, status={pos.status}")

            try:
                # 제한가 매도 지원 시 우선
                if hasattr(kis.order, "sell_limit"):
                    resp = kis.order.sell_limit(pos.code, int(pos.qty), int(limit_px_int))
                else:
                    resp = kis.order.sell_market(pos.code, int(pos.qty))

                print("  → 주문 응답:", (resp.get("msg1") if isinstance(resp, dict) else resp))

                if is_kis_order_ok(resp):
                    close_position(pos.id, exit_price=float(best_bid), exit_time=now.isoformat(), exit_reason="FORCE_LMT")
                else:
                    print(f"[HOLD] {pos.code} {pos.name}: 제한가 주문 실패/불확실 → 재시도")
            except Exception as e:
                print(f"[ERROR] {pos.code} {pos.name}: FORCE 제한가 실패: {e}")

        finally:
            _EXIT_INFLIGHT.discard(pos.id)


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
    config.end_date = last_report_day()
    csv_path = Path(config.price_report_dir) / f"Auto_Trading_{config.end_date}.csv"
    #csv_path = Path('C:/Users/30211/vs_code/YIL_TR_AUTO/Report_20251128/Trading_price_20251128.csv')
    
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
    # 결과 출력
    adjusted_signals = adjust_signals_based_on_trends(signals)
    for signal in adjusted_signals:
        print(f"종목: {signal['name']} ({signal['code']})")
        print(f"매수가: {signal['entry']}, SL: {signal['sl']}, TP: {signal['tp']}")
        print("---------")

    # 2) 신규 포지션 오픈
    
    open_new_positions_from_signals(kis, adjusted_signals)
    # 3) OPEN 포지션 TP/SL/만기 체크 및 청산
    process_open_positions(kis)


if __name__ == "__main__":
    main(sys.argv)



