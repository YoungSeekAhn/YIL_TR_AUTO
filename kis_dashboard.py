"""
trading_dashboard.py

YIL_AUTO_TRADING 실시간 모니터링용 대시보드 GUI

- 계좌 요약 (예수금, 평가금액, 총자산, 평가손익)
- 현재 OPEN 포지션 (DB + KIS 시세 → 현재가, 미실현손익)
- 금일 체결 포지션 (오늘 close된 포지션 → 실현손익)
- 로그 뷰어 (지정된 로그 파일 내용을 GUI에서 확인)

사용:
    python trading_dashboard.py
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional

import sqlite3

from kis_functions import KISAPI
from kis_pos_db import DB_PATH, get_open_positions, Position

# 로그 파일 경로 (필요에 따라 수정)
LOG_FILE_PATH = Path("yil_trading.log")


# ============================================================
# DB 헬퍼: 금일 체결 포지션 조회
# ============================================================

def fetch_today_closed_positions() -> List[Dict[str, Any]]:
    """
    오늘 날짜 기준으로 close_time 이 있는 포지션(청산 완료)을 조회.

    반환: dict 리스트
        - code, name, side, qty, entry, exit_price, open_time, close_time, exit_reason
        - pnl, pnl_pct (on-the-fly 계산)
    """
    rows_out: List[Dict[str, Any]] = []

    if not DB_PATH:
        return rows_out

    db_path = str(DB_PATH)
    if not os.path.exists(db_path):
        return rows_out

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        # close_time 이 NULL이 아니고, 날짜가 오늘인 포지션
        cur.execute(
            """
            SELECT *
            FROM positions
            WHERE close_time IS NOT NULL
              AND date(close_time) = date('now','localtime')
            ORDER BY close_time DESC
            """
        )
        for row in cur.fetchall():
            code = row["code"]
            name = row["name"]
            side = row["side"]
            qty = row["qty"]
            entry = row["entry"]
            exit_price = row["exit_price"]
            open_time = row["open_time"]
            close_time = row["close_time"]
            exit_reason = row["exit_reason"]

            # 실현 손익 및 수익률 계산 (DB에 없더라도 여기서 계산)
            if exit_price is not None and entry is not None and qty is not None:
                if side.upper() == "BUY":
                    pnl = (exit_price - entry) * qty
                else:  # 나중에 숏 도입 시 반대
                    pnl = (entry - exit_price) * qty
                pnl_pct = (exit_price - entry) / entry * 100.0 if entry != 0 else 0.0
            else:
                pnl = 0.0
                pnl_pct = 0.0

            rows_out.append(
                {
                    "code": code,
                    "name": name,
                    "side": side,
                    "qty": qty,
                    "entry": entry,
                    "exit_price": exit_price,
                    "open_time": open_time,
                    "close_time": close_time,
                    "exit_reason": exit_reason,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                }
            )
    finally:
        conn.close()

    return rows_out


# ============================================================
# 메인 GUI 클래스
# ============================================================

class TradingDashboard:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("YIL AUTO TRADING - DASHBOARD")

        # KIS API 초기화
        try:
            self.kis = KISAPI.from_env()
        except Exception as e:
            messagebox.showerror("KIS ERROR", f"KISAPI 초기화 실패:\n{e}")
            raise

        # 계좌 요약 바인딩 변수
        self.var_cash = tk.StringVar(value="예수금: -")
        self.var_eval_amt = tk.StringVar(value="평가금액(주식): -")
        self.var_total_asset = tk.StringVar(value="총자산: -")
        self.var_eval_pl = tk.StringVar(value="평가손익: -")
        self.var_open_count = tk.StringVar(value="OPEN 포지션 수: -")

        # 자동 새로고침 플래그
        self.auto_refresh = tk.BooleanVar(value=True)
        self.auto_refresh_interval_ms = 5000  # 5초마다

        # UI 구성
        self._build_ui()

        # 첫 로딩
        self.refresh_all()

        # 자동 새로고침 스케줄
        self._schedule_auto_refresh()

    # --------------------------------------------------------
    # UI 구성
    # --------------------------------------------------------
    def _build_ui(self):
        # 상단 요약 영역
        frame_top = ttk.Frame(self.root)
        frame_top.pack(fill="x", padx=10, pady=5)

        ttk.Label(frame_top, textvariable=self.var_cash).grid(
            row=0, column=0, sticky="w", padx=5, pady=2
        )
        ttk.Label(frame_top, textvariable=self.var_eval_amt).grid(
            row=0, column=1, sticky="w", padx=5, pady=2
        )
        ttk.Label(frame_top, textvariable=self.var_total_asset).grid(
            row=1, column=0, sticky="w", padx=5, pady=2
        )
        ttk.Label(frame_top, textvariable=self.var_eval_pl).grid(
            row=1, column=1, sticky="w", padx=5, pady=2
        )
        ttk.Label(frame_top, textvariable=self.var_open_count).grid(
            row=2, column=0, sticky="w", padx=5, pady=2
        )

        # 새로고침 / 자동 새로고침 버튼
        frame_ctrl = ttk.Frame(self.root)
        frame_ctrl.pack(fill="x", padx=10, pady=5)

        btn_refresh = ttk.Button(frame_ctrl, text="지금 새로고침", command=self.refresh_all)
        btn_refresh.pack(side="left", padx=5)

        chk_auto = ttk.Checkbutton(
            frame_ctrl,
            text="자동 새로고침 (5초)",
            variable=self.auto_refresh,
        )
        chk_auto.pack(side="left", padx=5)

        # 중앙: Notebook (탭)
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=5)

        # 탭 1: OPEN 포지션
        self.frame_open = ttk.Frame(nb)
        nb.add(self.frame_open, text="현재 보유 포지션")

        self._build_open_positions_tab(self.frame_open)

        # 탭 2: 금일 체결 내역
        self.frame_today = ttk.Frame(nb)
        nb.add(self.frame_today, text="금일 체결 내역")

        self._build_today_trades_tab(self.frame_today)

        # 하단: 로그 뷰어
        frame_log = ttk.LabelFrame(self.root, text="로그 (yil_trading.log)")
        frame_log.pack(fill="both", expand=True, padx=10, pady=5)

        self.log_area = scrolledtext.ScrolledText(frame_log, height=10)
        self.log_area.pack(fill="both", expand=True)

    def _build_open_positions_tab(self, parent: ttk.Frame):
        cols = (
            "code",
            "name",
            "side",
            "qty",
            "entry",
            "cur_price",
            "tp",
            "sl",
            "unreal_pnl",
            "unreal_pnl_pct",
            "open_time",
            "status",
        )
        self.tree_open = ttk.Treeview(parent, columns=cols, show="headings", height=10)

        self.tree_open.heading("code", text="종목코드")
        self.tree_open.heading("name", text="종목명")
        self.tree_open.heading("side", text="매매구분")
        self.tree_open.heading("qty", text="수량")
        self.tree_open.heading("entry", text="매수가")
        self.tree_open.heading("cur_price", text="현재가")
        self.tree_open.heading("tp", text="TP")
        self.tree_open.heading("sl", text="SL")
        self.tree_open.heading("unreal_pnl", text="미실현손익")
        self.tree_open.heading("unreal_pnl_pct", text="미실현 수익률(%)")
        self.tree_open.heading("open_time", text="진입시간")
        self.tree_open.heading("status", text="상태")

        self.tree_open.column("code", width=80)
        self.tree_open.column("name", width=120)
        self.tree_open.column("side", width=60, anchor="center")
        self.tree_open.column("qty", width=60, anchor="e")
        self.tree_open.column("entry", width=80, anchor="e")
        self.tree_open.column("cur_price", width=80, anchor="e")
        self.tree_open.column("tp", width=80, anchor="e")
        self.tree_open.column("sl", width=80, anchor="e")
        self.tree_open.column("unreal_pnl", width=100, anchor="e")
        self.tree_open.column("unreal_pnl_pct", width=110, anchor="e")
        self.tree_open.column("open_time", width=150)
        self.tree_open.column("status", width=70, anchor="center")

        self.tree_open.pack(fill="both", expand=True)

    def _build_today_trades_tab(self, parent: ttk.Frame):
        cols = (
            "code",
            "name",
            "side",
            "qty",
            "entry",
            "exit_price",
            "pnl",
            "pnl_pct",
            "open_time",
            "close_time",
            "exit_reason",
        )
        self.tree_today = ttk.Treeview(parent, columns=cols, show="headings", height=10)

        self.tree_today.heading("code", text="종목코드")
        self.tree_today.heading("name", text="종목명")
        self.tree_today.heading("side", text="매매구분")
        self.tree_today.heading("qty", text="수량")
        self.tree_today.heading("entry", text="매수가")
        self.tree_today.heading("exit_price", text="청산가")
        self.tree_today.heading("pnl", text="실현손익")
        self.tree_today.heading("pnl_pct", text="수익률(%)")
        self.tree_today.heading("open_time", text="진입시간")
        self.tree_today.heading("close_time", text="청산시간")
        self.tree_today.heading("exit_reason", text="청산사유")

        self.tree_today.column("code", width=80)
        self.tree_today.column("name", width=120)
        self.tree_today.column("side", width=60, anchor="center")
        self.tree_today.column("qty", width=60, anchor="e")
        self.tree_today.column("entry", width=80, anchor="e")
        self.tree_today.column("exit_price", width=80, anchor="e")
        self.tree_today.column("pnl", width=100, anchor="e")
        self.tree_today.column("pnl_pct", width=90, anchor="e")
        self.tree_today.column("open_time", width=150)
        self.tree_today.column("close_time", width=150)
        self.tree_today.column("exit_reason", width=90)

        self.tree_today.pack(fill="both", expand=True)

    # --------------------------------------------------------
    # 새로고침 로직 (Worker Thread + UI 업데이트)
    # --------------------------------------------------------
    def refresh_all(self):
        """버튼/자동 호출 시 전체 데이터 새로고침 (별도 스레드에서 실행)."""
        threading.Thread(target=self._refresh_all_worker, daemon=True).start()

    def _refresh_all_worker(self):
        try:
            # 1) 계좌 요약
            summary = self.kis.account.get_summary()

            # 2) OPEN 포지션 (DB)
            open_positions = get_open_positions()

            # 3) 각 포지션별 현재가 조회
            open_rows = self._build_open_positions_view(open_positions)

            # 4) 금일 체결 포지션
            today_trades = fetch_today_closed_positions()

            # 5) 로그 파일 읽기
            logs_text = self._read_log_tail()

            # UI 업데이트는 main thread 에서
            self.root.after(
                0,
                self._update_ui,
                summary,
                open_rows,
                today_trades,
                logs_text,
            )
        except Exception as e:
            self.root.after(0, self._handle_refresh_error, e)

    def _build_open_positions_view(
        self, positions: List[Position]
    ) -> List[Dict[str, Any]]:
        """
        DB 포지션 + KIS 현재가를 합쳐서
        GUI에 바로 뿌릴 수 있는 dict 리스트로 변환.
        """
        rows: List[Dict[str, Any]] = []

        for pos in positions:
            code = pos.code
            name = pos.name
            side = pos.side
            qty = pos.qty or 0
            entry = pos.entry or 0.0
            tp = pos.tp
            sl = pos.sl
            open_time = pos.open_time
            status = pos.status

            # 현재가 조회
            try:
                q = self.kis.market.get_quote(code)
                out = q.get("output", {})
                cur_price = float(out.get("stck_prpr", "0") or 0.0)
            except Exception as e:
                print(f"[WARN] 현재가 조회 실패: {code} {name}: {e}")
                cur_price = 0.0

            # 미실현 손익 계산
            if cur_price > 0 and entry > 0 and qty > 0:
                if side.upper() == "BUY":
                    unreal_pnl = (cur_price - entry) * qty
                else:
                    unreal_pnl = (entry - cur_price) * qty
                unreal_pnl_pct = (cur_price - entry) / entry * 100.0
            else:
                unreal_pnl = 0.0
                unreal_pnl_pct = 0.0

            rows.append(
                {
                    "code": code,
                    "name": name,
                    "side": side,
                    "qty": qty,
                    "entry": entry,
                    "cur_price": cur_price,
                    "tp": tp,
                    "sl": sl,
                    "unreal_pnl": unreal_pnl,
                    "unreal_pnl_pct": unreal_pnl_pct,
                    "open_time": open_time,
                    "status": status,
                }
            )

        return rows

    def _read_log_tail(self, max_bytes: int = 20000) -> str:
        """
        로그 파일의 마지막 일부만 읽어서 문자열로 반환.
        - 파일이 너무 크더라도 뒤에서부터 max_bytes 만큼만 읽음.
        """
        if not LOG_FILE_PATH.exists():
            return "(로그 파일이 없습니다: {})".format(LOG_FILE_PATH)

        try:
            size = LOG_FILE_PATH.stat().st_size
            with LOG_FILE_PATH.open("rb") as f:
                if size > max_bytes:
                    f.seek(size - max_bytes)
                data = f.read().decode("utf-8", errors="replace")
            return data
        except Exception as e:
            return f"(로그 읽기 오류: {e})"

    # --------------------------------------------------------
    # UI 업데이트
    # --------------------------------------------------------
    def _update_ui(
        self,
        summary: Dict[str, Any],
        open_rows: List[Dict[str, Any]],
        today_trades: List[Dict[str, Any]],
        logs_text: str,
    ):
        # 계좌 요약
        cash = float(summary.get("cash", 0.0) or 0.0)
        eval_amt = float(summary.get("eval_amount", 0.0) or 0.0)
        total_asset = float(summary.get("total_asset", 0.0) or 0.0)
        eval_pl = float(summary.get("eval_pl", 0.0) or 0.0)

        self.var_cash.set(f"예수금: {cash:,.0f}원")
        self.var_eval_amt.set(f"평가금액(주식): {eval_amt:,.0f}원")
        self.var_total_asset.set(f"총자산(추정): {total_asset:,.0f}원")
        self.var_eval_pl.set(f"평가손익(자산증감): {eval_pl:,.0f}원")
        self.var_open_count.set(f"OPEN 포지션 수: {len(open_rows)}개")

        # OPEN 포지션 테이블 갱신
        for row_id in self.tree_open.get_children():
            self.tree_open.delete(row_id)

        for r in open_rows:
            self.tree_open.insert(
                "",
                tk.END,
                values=(
                    r["code"],
                    r["name"],
                    r["side"],
                    f"{r['qty']}",
                    f"{r['entry']:,.0f}",
                    f"{r['cur_price']:,.0f}",
                    "" if r["tp"] is None else f"{r['tp']:,.0f}",
                    "" if r["sl"] is None else f"{r['sl']:,.0f}",
                    f"{r['unreal_pnl']:,.0f}",
                    f"{r['unreal_pnl_pct']:.2f}",
                    r["open_time"],
                    r["status"],
                ),
            )

        # 금일 체결 내역 테이블 갱신
        for row_id in self.tree_today.get_children():
            self.tree_today.delete(row_id)

        for r in today_trades:
            self.tree_today.insert(
                "",
                tk.END,
                values=(
                    r["code"],
                    r["name"],
                    r["side"],
                    f"{r['qty']}",
                    f"{r['entry']:,.0f}" if r["entry"] else "",
                    f"{r['exit_price']:,.0f}" if r["exit_price"] else "",
                    f"{r['pnl']:,.0f}",
                    f"{r['pnl_pct']:.2f}",
                    r["open_time"],
                    r["close_time"],
                    r["exit_reason"],
                ),
            )

        # 로그 영역 갱신
        self.log_area.delete("1.0", tk.END)
        self.log_area.insert(tk.END, logs_text)
        self.log_area.see(tk.END)

    def _handle_refresh_error(self, err: Exception):
        msg = f"대시보드 새로고침 중 오류 발생:\n{err}"
        print("[ERROR]", msg)
        # 너무 자주 팝업 뜨면 귀찮으니까, 여기서는 콘솔에만 남기고
        # 필요하면 messagebox 로 바꿀 수 있음.

    # --------------------------------------------------------
    # 자동 새로고침 스케줄
    # --------------------------------------------------------
    def _schedule_auto_refresh(self):
        """auto_refresh 가 True 인 동안 주기적으로 refresh_all 호출."""
        if self.auto_refresh.get():
            self.refresh_all()
        self.root.after(self.auto_refresh_interval_ms, self._schedule_auto_refresh)


# ============================================================
# main
# ============================================================

if __name__ == "__main__":
    root = tk.Tk()
    app = TradingDashboard(root)
    root.mainloop()
