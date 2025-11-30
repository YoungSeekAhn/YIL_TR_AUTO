"""
kis_pos_db.py
----------------
YIL_AUTO_TRADE 포지션 관리용 SQLite 모듈.

- 포지션 단위(한 번 매수 → 한 번 청산)를 기록
- 진입 시: CSV 메타 정보 + entry/tp/sl 저장
- 청산 시: 수익금, 수익률, 실제 보유일수 등 기록
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List, Dict, Any

DB_PATH = "yil_trading.db"


# ============================================================
# Dataclass (선택적 사용용)
# ============================================================

@dataclass
class Position:
    id: Optional[int]
    code: str
    name: str
    side: str
    qty: int
    entry: float
    tp: Optional[float]
    sl: Optional[float]
    open_time: str
    close_time: Optional[str]
    status: str
    exit_price: Optional[float]
    exit_reason: Optional[str]
    score_1w: Optional[float]
    rr: Optional[float]
    confidence: Optional[float]
    horizon: Optional[str]
    holding_days_plan: Optional[int]
    risk_cap_used: Optional[float]
    valid_until: Optional[str]
    source_file: Optional[str]
    summary_h1: Optional[float]
    summary_h2: Optional[float]
    summary_h3: Optional[float]
    warn_bad_rr: Optional[int]
    gross_pnl: Optional[float]
    pnl_pct: Optional[float]
    holding_days_real: Optional[float]
    note: Optional[str]


# ============================================================
# DB 초기화 / 연결
# ============================================================

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    positions 테이블이 없으면 생성.
    """
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS positions (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,

            -- 기본 포지션 정보
            code                TEXT NOT NULL,
            name                TEXT NOT NULL,
            side                TEXT NOT NULL,
            qty                 INTEGER NOT NULL,
            entry               REAL NOT NULL,
            tp                  REAL,
            sl                  REAL,
            open_time           TEXT NOT NULL,   -- ISO8601 문자열
            close_time          TEXT,
            status              TEXT NOT NULL,   -- OPEN / CLOSED
            exit_price          REAL,
            exit_reason         TEXT,

            -- CSV 기반 메타 정보
            score_1w            REAL,
            rr                  REAL,
            confidence          REAL,
            horizon             TEXT,
            holding_days_plan   INTEGER,
            risk_cap_used       REAL,
            valid_until         TEXT,
            source_file         TEXT,
            summary_h1          REAL,
            summary_h2          REAL,
            summary_h3          REAL,
            warn_bad_rr         INTEGER,

            -- 성능 지표
            gross_pnl           REAL,
            pnl_pct             REAL,
            holding_days_real   REAL,

            -- 기타
            note                TEXT
        );
        """
    )

    conn.commit()
    conn.close()


# ============================================================
# OPEN 포지션 관련 함수
# ============================================================

def add_open_position_from_csv_row(
    row: Dict[str, Any],
    qty: int,
    entry_price: float,
    open_time_iso: Optional[str] = None,
) -> int:
    """
    CSV 한 줄(row) + 실제 체결 정보(qty, entry_price)를 받아
    positions 테이블에 OPEN 포지션을 추가.

    row: pandas.DataFrame.iloc[...] 등에서 dict(row) 형태로 넘기는 것을 권장.
    """
    if open_time_iso is None:
        open_time_iso = datetime.now().isoformat(timespec="seconds")

    code = str(row["종목코드"]).strip()
    name = str(row["종목명"]).strip()

    # 컬럼 이름 상수 (CSV 헤더와 정확히 일치해야 함)
    ENTRY_COL = "매수가(entry)"
    TP_COL = "익절가(tp)"
    SL_COL = "손절가(sl)"

    tp = float(row.get(TP_COL)) if row.get(TP_COL) not in (None, "",) else None
    sl = float(row.get(SL_COL)) if row.get(SL_COL) not in (None, "",) else None

    def _to_float_safe(val):
        try:
            if val is None or val == "":
                return None
            return float(val)
        except Exception:
            return None

    def _to_int_safe(val):
        try:
            if val is None or val == "":
                return None
            return int(val)
        except Exception:
            return None

    score_1w = _to_float_safe(row.get("Score_1w"))
    rr = _to_float_safe(row.get("RR"))
    confidence = _to_float_safe(row.get("confidence"))
    holding_days_plan = _to_int_safe(row.get("holding_days"))
    risk_cap_used = _to_float_safe(row.get("risk_cap_used"))
    summary_h1 = _to_float_safe(row.get("요약점수(h1)"))
    summary_h2 = _to_float_safe(row.get("요약점수(h2)"))
    summary_h3 = _to_float_safe(row.get("요약점수(h3)"))

    warn_bad_rr_raw = row.get("warn_bad_RR")
    if warn_bad_rr_raw in (None, "", "0"):
        warn_bad_rr = 0
    else:
        # 단순히 0/1 플래그로만 활용
        warn_bad_rr = 1

    horizon = str(row.get("권장호라이즌") or "")
    valid_until = str(row.get("valid_until") or "")
    source_file = str(row.get("source_file") or "")

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO positions (
            code, name, side, qty, entry, tp, sl, open_time, status,
            score_1w, rr, confidence, horizon, holding_days_plan, risk_cap_used,
            valid_until, source_file, summary_h1, summary_h2, summary_h3,
            warn_bad_rr, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            code,
            name,
            "BUY",
            int(qty),
            float(entry_price),
            tp,
            sl,
            open_time_iso,
            "OPEN",
            score_1w,
            rr,
            confidence,
            horizon,
            holding_days_plan,
            risk_cap_used,
            valid_until,
            source_file,
            summary_h1,
            summary_h2,
            summary_h3,
            warn_bad_rr,
            "",
        ),
    )

    position_id = cur.lastrowid
    conn.commit()
    conn.close()
    return position_id


def get_open_positions() -> List[Position]:
    """
    status = 'OPEN' 인 포지션들을 모두 반환.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM positions WHERE status = 'OPEN'")
    rows = cur.fetchall()
    conn.close()

    positions: List[Position] = []
    for r in rows:
        positions.append(
            Position(
                id=r["id"],
                code=r["code"],
                name=r["name"],
                side=r["side"],
                qty=r["qty"],
                entry=r["entry"],
                tp=r["tp"],
                sl=r["sl"],
                open_time=r["open_time"],
                close_time=r["close_time"],
                status=r["status"],
                exit_price=r["exit_price"],
                exit_reason=r["exit_reason"],
                score_1w=r["score_1w"],
                rr=r["rr"],
                confidence=r["confidence"],
                horizon=r["horizon"],
                holding_days_plan=r["holding_days_plan"],
                risk_cap_used=r["risk_cap_used"],
                valid_until=r["valid_until"],
                source_file=r["source_file"],
                summary_h1=r["summary_h1"],
                summary_h2=r["summary_h2"],
                summary_h3=r["summary_h3"],
                warn_bad_rr=r["warn_bad_rr"],
                gross_pnl=r["gross_pnl"],
                pnl_pct=r["pnl_pct"],
                holding_days_real=r["holding_days_real"],
                note=r["note"],
            )
        )
    return positions


def get_open_codes() -> List[str]:
    """
    OPEN 포지션들의 종목코드 리스트 반환.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT code FROM positions WHERE status = 'OPEN'")
    rows = cur.fetchall()
    conn.close()
    return [r["code"] for r in rows]


# ============================================================
# 청산 관련 함수
# ============================================================

def close_positions_for_code(
    code: str,
    exit_price: float,
    exit_time_iso: Optional[str] = None,
    reason: str = "manual",
) -> None:
    """
    특정 code에 대해 status='OPEN'인 모든 포지션을
    exit_price/exit_time/exit_reason으로 마킹하고 CLOSED로 바꿈.
    """
    if exit_time_iso is None:
        exit_time_iso = datetime.now().isoformat(timespec="seconds")

    conn = get_connection()
    cur = conn.cursor()

    # 우선 entry/qty/open_time 읽어와서 수익 계산
    cur.execute(
        "SELECT id, entry, qty, open_time FROM positions WHERE status = 'OPEN' AND code = ?",
        (code,),
    )
    rows = cur.fetchall()

    for r in rows:
        pos_id = r["id"]
        entry = float(r["entry"])
        qty = int(r["qty"])
        open_time = r["open_time"]

        gross_pnl = (exit_price - entry) * qty
        pnl_pct = (exit_price - entry) / entry * 100.0

        try:
            dt_open = datetime.fromisoformat(open_time)
            dt_close = datetime.fromisoformat(exit_time_iso)
            holding_days_real = (dt_close - dt_open).total_seconds() / 86400.0
        except Exception:
            holding_days_real = None

        cur.execute(
            """
            UPDATE positions
               SET status = 'CLOSED',
                   close_time = ?,
                   exit_price = ?,
                   exit_reason = ?,
                   gross_pnl = ?,
                   pnl_pct = ?,
                   holding_days_real = ?
             WHERE id = ?
            """,
            (
                exit_time_iso,
                float(exit_price),
                reason,
                gross_pnl,
                pnl_pct,
                holding_days_real,
                pos_id,
            ),
        )

    conn.commit()
    conn.close()


def mark_missing_codes_closed(current_held_codes: List[str]) -> None:
    """
    DB에는 OPEN인데, 실제 계좌에는 더 이상 없는 종목을
    'external' 청산으로 마킹하는 용도 (선택적 사용).

    current_held_codes: 실제 계좌 보유 종목 코드 리스트
    """
    held_set = set(current_held_codes)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, code, open_time, entry, qty FROM positions WHERE status = 'OPEN'")
    rows = cur.fetchall()

    now_iso = datetime.now().isoformat(timespec="seconds")

    for r in rows:
        code = r["code"]
        if code in held_set:
            continue

        # 계좌에 없는데 DB엔 OPEN → 외부 청산으로 가정
        entry = float(r["entry"])
        qty = int(r["qty"])
        open_time = r["open_time"]
        pos_id = r["id"]

        # exit_price는 계좌상 알 수 없으므로 None / entry로 둘 수도 있음
        exit_price = None
        gross_pnl = None
        pnl_pct = None
        holding_days_real = None
        try:
            dt_open = datetime.fromisoformat(open_time)
            dt_close = datetime.fromisoformat(now_iso)
            holding_days_real = (dt_close - dt_open).total_seconds() / 86400.0
        except Exception:
            pass

        cur.execute(
            """
            UPDATE positions
               SET status = 'CLOSED',
                   close_time = ?,
                   exit_price = ?,
                   exit_reason = ?,
                   gross_pnl = ?,
                   pnl_pct = ?,
                   holding_days_real = ?
             WHERE id = ?
            """,
            (
                now_iso,
                exit_price,
                "external",
                gross_pnl,
                pnl_pct,
                holding_days_real,
                pos_id,
            ),
        )

    conn.commit()
    conn.close()
