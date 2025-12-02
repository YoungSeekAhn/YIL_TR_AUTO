"""
kis_pos_db.py
KIS 자동매매 포지션(SQLite) 관리 모듈

- Position dataclass (트레이딩 포지션 1건)
- SQLite 테이블 'positions' 생성/조회/갱신
- 매매 완료 시 실현손익, 수익률, 보유기간까지 기록
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 기본 DB 경로 (프로젝트 루트 기준 필요하면 수정)
DB_PATH = Path(__file__).with_name("kis_positions.db")


# ============================================================
# Dataclass: Position
# ============================================================

@dataclass
class Position:
    id: Optional[int]
    code: str
    name: str
    side: str              # "BUY" / "SELL" 등
    qty: int
    entry: float           # 진입가
    tp: Optional[float]
    sl: Optional[float]
    open_time: str         # ISO 문자열 (예: "2025-12-02T09:05:00+09:00")
    close_time: Optional[str]
    status: str            # "OPEN" / "CLOSED"
    exit_price: Optional[float]
    exit_reason: Optional[str]
    score_1w: Optional[float]
    rr: Optional[float]
    confidence: Optional[float]
    horizon: Optional[str]
    valid_until: Optional[str]
    note: Optional[str]    # 비고


# ============================================================
# DB 초기화 / 연결 헬퍼
# ============================================================

def get_connection(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """
    SQLite 커넥션 생성 (row_factory를 dict처럼 쓰기 편하게 설정)
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | str = DB_PATH) -> None:
    """
    positions 테이블 생성 (이미 있으면 무시)
    - Position 필드 + 분석용 컬럼(실현손익, 수익률, 보유기간 등)
    """
    conn = get_connection(db_path)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS positions (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            code                TEXT NOT NULL,
            name                TEXT NOT NULL,
            side                TEXT NOT NULL,
            qty                 INTEGER NOT NULL,
            entry               REAL NOT NULL,
            tp                  REAL,
            sl                  REAL,
            open_time           TEXT NOT NULL,
            close_time          TEXT,
            status              TEXT NOT NULL,         -- "OPEN" or "CLOSED"
            exit_price          REAL,
            exit_reason         TEXT,
            score_1w            REAL,
            rr                  REAL,
            confidence          REAL,
            horizon             TEXT,
            valid_until         TEXT,
            note                TEXT,

            -- 분석용 추가 컬럼 (매매 완료 후 채워짐)
            realized_pnl        REAL,                 -- 실현손익 금액
            realized_pnl_rate   REAL,                 -- 실현손익률 (exit_amount / entry_amount - 1)
            holding_days        REAL,                 -- 보유기간 (일 단위, float)
            entry_amount        REAL,                 -- 진입금액 = qty * entry
            exit_amount         REAL                  -- 청산금액 = qty * exit_price
        )
        """
    )

    conn.commit()
    conn.close()


# ============================================================
# Row ↔ Position 변환 헬퍼
# ============================================================

def row_to_position(row: sqlite3.Row) -> Position:
    """
    DB row → Position dataclass로 변환
    (분석용 컬럼들은 dataclass에 포함하지 않고 row로 직접 접근 가능)
    """
    return Position(
        id=row["id"],
        code=row["code"],
        name=row["name"],
        side=row["side"],
        qty=row["qty"],
        entry=row["entry"],
        tp=row["tp"],
        sl=row["sl"],
        open_time=row["open_time"],
        close_time=row["close_time"],
        status=row["status"],
        exit_price=row["exit_price"],
        exit_reason=row["exit_reason"],
        score_1w=row["score_1w"],
        rr=row["rr"],
        confidence=row["confidence"],
        horizon=row["horizon"],
        valid_until=row["valid_until"],
        note=row["note"],
    )


def position_to_db_params(pos: Position) -> Dict[str, Any]:
    """
    Position → INSERT/UPDATE용 dict (분석용 컬럼은 여기서 제외)
    """
    return {
        "code": pos.code,
        "name": pos.name,
        "side": pos.side,
        "qty": pos.qty,
        "entry": pos.entry,
        "tp": pos.tp,
        "sl": pos.sl,
        "open_time": pos.open_time,
        "close_time": pos.close_time,
        "status": pos.status,
        "exit_price": pos.exit_price,
        "exit_reason": pos.exit_reason,
        "score_1w": pos.score_1w,
        "rr": pos.rr,
        "confidence": pos.confidence,
        "horizon": pos.horizon,
        "valid_until": pos.valid_until,
        "note": pos.note,
    }


# ============================================================
# INSERT (진입 시 기록)
# ============================================================

def insert_position(pos: Position, db_path: Path | str = DB_PATH) -> int:
    """
    새 포지션(매수/매도 진입)을 DB에 기록.
    - status는 "OPEN" 으로 넣는 것을 기본 가정.
    - 분석용 컬럼은 아직 None으로 두고, 추후 close_position에서 계산.
    반환값: 생성된 id
    """
    conn = get_connection(db_path)
    cur = conn.cursor()

    params = position_to_db_params(pos)
    cur.execute(
        """
        INSERT INTO positions
        (
            code, name, side, qty, entry, tp, sl,
            open_time, close_time, status,
            exit_price, exit_reason,
            score_1w, rr, confidence, horizon, valid_until, note,
            realized_pnl, realized_pnl_rate, holding_days,
            entry_amount, exit_amount
        )
        VALUES
        (
            :code, :name, :side, :qty, :entry, :tp, :sl,
            :open_time, :close_time, :status,
            :exit_price, :exit_reason,
            :score_1w, :rr, :confidence, :horizon, :valid_until, :note,
            NULL, NULL, NULL,
            NULL, NULL
        )
        """,
        params,
    )

    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return new_id


# ============================================================
# UPDATE: 포지션 청산 (수익금·수익률·보유기간 계산)
# ============================================================

def close_position(
    pos_id: int,
    exit_price: float,
    exit_time: str,
    exit_reason: str,
    db_path: Path | str = DB_PATH,
) -> None:
    """
    포지션을 '청산' 처리하고, 실현손익/수익률/보유기간까지 계산해 DB에 저장.

    - exit_price: 청산 가격
    - exit_time : 청산 시간 (ISO 문자열)
    - exit_reason: "TP", "SL", "TIMEOUT", "MANUAL" 등
    """

    conn = get_connection(db_path)
    cur = conn.cursor()

    # 1) 기존 포지션 정보 가져오기
    cur.execute("SELECT * FROM positions WHERE id = ?", (pos_id,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        raise ValueError(f"Position id={pos_id} not found")

    qty = row["qty"]
    entry = row["entry"]
    open_time = row["open_time"]
    side = row["side"]

    # 2) 금액 계산
    entry_amount = float(entry) * float(qty)
    exit_amount = float(exit_price) * float(qty)

    # LONG 기준 실현손익; side가 SELL(숏)일 경우 방향 반전
    pnl = exit_amount - entry_amount
    if side.upper() == "SELL":
        pnl = -pnl

    pnl_rate = pnl / entry_amount if entry_amount != 0 else None

    # 3) 보유기간 계산 (open_time, exit_time 둘 다 ISO 문자열 가정)
    holding_days: Optional[float]
    try:
        t_open = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
        t_close = datetime.fromisoformat(exit_time.replace("Z", "+00:00"))
        holding_days = (t_close - t_open).total_seconds() / 86400.0
    except Exception:
        holding_days = None

    # 4) DB 업데이트
    cur.execute(
        """
        UPDATE positions
        SET
            close_time        = ?,
            status            = 'CLOSED',
            exit_price        = ?,
            exit_reason       = ?,
            realized_pnl      = ?,
            realized_pnl_rate = ?,
            holding_days      = ?,
            entry_amount      = ?,
            exit_amount       = ?
        WHERE id = ?
        """,
        (
            exit_time,
            exit_price,
            exit_reason,
            pnl,
            pnl_rate,
            holding_days,
            entry_amount,
            exit_amount,
            pos_id,
        ),
    )

    conn.commit()
    conn.close()


# ============================================================
# 조회 유틸
# ============================================================

def get_open_positions(db_path: Path | str = DB_PATH) -> List[Position]:
    """
    status = 'OPEN' 인 포지션 리스트 반환
    """
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY open_time ASC"
    )
    rows = cur.fetchall()
    conn.close()
    return [row_to_position(r) for r in rows]


def get_all_positions(db_path: Path | str = DB_PATH) -> List[Position]:
    """
    전체 포지션 리스트 반환 (OPEN + CLOSED)
    """
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT * FROM positions ORDER BY open_time DESC, id DESC")
    rows = cur.fetchall()
    conn.close()
    return [row_to_position(r) for r in rows]


def get_position_by_id(pos_id: int, db_path: Path | str = DB_PATH) -> Optional[Position]:
    """
    id로 단일 포지션 조회 (없으면 None)
    """
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT * FROM positions WHERE id = ?", (pos_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None
    return row_to_position(row)


# ============================================================
# 비고 / 메모 수정
# ============================================================

def update_note(pos_id: int, note: Optional[str], db_path: Path | str = DB_PATH) -> None:
    """
    포지션의 note(비고) 필드 업데이트
    """
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute(
        "UPDATE positions SET note = ? WHERE id = ?",
        (note, pos_id),
    )
    conn.commit()
    conn.close()


# ============================================================
# 모듈 단독 실행 테스트
# ============================================================

if __name__ == "__main__":
    print(f"[INFO] DB init at: {DB_PATH}")
    init_db()

    # 예시: 더미 포지션 하나 넣어보기
    demo = Position(
        id=None,
        code="005930",
        name="삼성전자",
        side="BUY",
        qty=10,
        entry=70000.0,
        tp=73000.0,
        sl=68000.0,
        open_time=datetime.now().isoformat(),
        close_time=None,
        status="OPEN",
        exit_price=None,
        exit_reason=None,
        score_1w=150.0,
        rr=2.5,
        confidence=0.6,
        horizon="h2",
        valid_until=None,
        note="테스트 포지션",
    )
    pid = insert_position(demo)
    print(f"[INFO] inserted demo position id={pid}")

    # OPEN 포지션 조회
    open_pos = get_open_positions()
    print(f"[INFO] open positions: {len(open_pos)}")
    for p in open_pos:
        print("  ", p)
