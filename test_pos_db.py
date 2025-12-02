"""
test_pos_db.py
kis_pos_db에서 관리하는 포지션 DB(kis_positions.db)를
간단하게 확인하기 위한 Tkinter GUI

- 상단: 필터 버튼 (전체, OPEN, CLOSED, 새로고침)
- 중앙: positions 목록 (Treeview)
- 하단: 선택된 포지션 상세 내용
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import sqlite3
from typing import List

try:
    import kis_pos_db
except ImportError:
    kis_pos_db = None


# ---------------------------------------------------------
# DB 접근 유틸
# ---------------------------------------------------------

def get_db_path() -> str:
    if kis_pos_db is not None and hasattr(kis_pos_db, "DB_PATH"):
        return str(kis_pos_db.DB_PATH)
    # fallback
    return "kis_positions.db"


def get_connection() -> sqlite3.Connection:
    if kis_pos_db is not None and hasattr(kis_pos_db, "get_connection"):
        return kis_pos_db.get_connection(get_db_path())
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------
# GUI 클래스
# ---------------------------------------------------------

class PosDBViewer:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("KIS Positions DB Viewer")

        self.conn = None
        self._connect_db()

        self.current_filter = "ALL"  # ALL / OPEN / CLOSED

        self._build_ui()
        self.refresh_table()

    # -----------------------------
    # DB 연결
    # -----------------------------
    def _connect_db(self):
        try:
            self.conn = get_connection()
        except Exception as e:
            messagebox.showerror("DB ERROR", f"DB 접속 실패:\n{e}")
            self.conn = None

    # -----------------------------
    # UI 구성
    # -----------------------------
    def _build_ui(self):
        # 상단 필터/버튼 영역
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=10, pady=5)

        self.lbl_db = ttk.Label(top, text=f"DB: {get_db_path()}")
        self.lbl_db.pack(side="left")

        btn_all = ttk.Button(top, text="전체", command=lambda: self._set_filter("ALL"))
        btn_all.pack(side="right", padx=2)

        btn_closed = ttk.Button(top, text="CLOSED만", command=lambda: self._set_filter("CLOSED"))
        btn_closed.pack(side="right", padx=2)

        btn_open = ttk.Button(top, text="OPEN만", command=lambda: self._set_filter("OPEN"))
        btn_open.pack(side="right", padx=2)

        btn_refresh = ttk.Button(top, text="새로고침", command=self.refresh_table)
        btn_refresh.pack(side="right", padx=2)

        # 중앙: 테이블
        frame_table = ttk.LabelFrame(self.root, text="Positions 테이블")
        frame_table.pack(fill="both", expand=True, padx=10, pady=5)

        cols = [
            "id",
            "code",
            "name",
            "side",
            "qty",
            "entry",
            "tp",
            "sl",
            "open_time",
            "close_time",
            "status",
            "exit_price",
            "realized_pnl",
            "realized_pnl_rate",
            "holding_days",
            "note",
        ]
        self.tree = ttk.Treeview(frame_table, columns=cols, show="headings", height=12)

        col_titles = {
            "id": "ID",
            "code": "종목코드",
            "name": "종목명",
            "side": "매매",
            "qty": "수량",
            "entry": "진입가",
            "tp": "TP",
            "sl": "SL",
            "open_time": "진입시간",
            "close_time": "청산시간",
            "status": "상태",
            "exit_price": "청산가",
            "realized_pnl": "실현손익",
            "realized_pnl_rate": "수익률",
            "holding_days": "보유일수",
            "note": "비고",
        }

        for c in cols:
            self.tree.heading(c, text=col_titles.get(c, c))

        # 대략적인 너비 설정
        self.tree.column("id", width=40, anchor="e")
        self.tree.column("code", width=70)
        self.tree.column("name", width=120)
        self.tree.column("side", width=50, anchor="center")
        self.tree.column("qty", width=60, anchor="e")
        self.tree.column("entry", width=80, anchor="e")
        self.tree.column("tp", width=80, anchor="e")
        self.tree.column("sl", width=80, anchor="e")
        self.tree.column("open_time", width=140)
        self.tree.column("close_time", width=140)
        self.tree.column("status", width=70, anchor="center")
        self.tree.column("exit_price", width=80, anchor="e")
        self.tree.column("realized_pnl", width=90, anchor="e")
        self.tree.column("realized_pnl_rate", width=80, anchor="e")
        self.tree.column("holding_days", width=80, anchor="e")
        self.tree.column("note", width=150)

        self.tree.pack(side="left", fill="both", expand=True)

        # 스크롤바
        scrollbar = ttk.Scrollbar(frame_table, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.bind("<<TreeviewSelect>>", self.on_row_selected)

        # 하단: 상세 정보
        frame_detail = ttk.LabelFrame(self.root, text="선택된 포지션 상세")
        frame_detail.pack(fill="both", expand=True, padx=10, pady=5)

        self.detail_text = scrolledtext.ScrolledText(frame_detail, height=8)
        self.detail_text.pack(fill="both", expand=True)

    # -----------------------------
    # 필터 변경
    # -----------------------------
    def _set_filter(self, mode: str):
        self.current_filter = mode
        self.refresh_table()

    # -----------------------------
    # 테이블 새로고침
    # -----------------------------
    def refresh_table(self):
        if self.conn is None:
            self._connect_db()
            if self.conn is None:
                return

        # 테이블 존재 여부 확인
        try:
            cur = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='positions'"
            )
            row = cur.fetchone()
            if not row:
                messagebox.showinfo(
                    "정보",
                    "'positions' 테이블이 존재하지 않습니다.\n"
                    "kis_pos_db.init_db()를 먼저 실행했는지 확인하세요.",
                )
                return
        except Exception as e:
            messagebox.showerror("DB ERROR", f"테이블 확인 중 오류:\n{e}")
            return

        # 기존 row 삭제
        for r in self.tree.get_children():
            self.tree.delete(r)

        # 필터에 따라 조회
        where_clause = ""
        params: List[str] = []

        if self.current_filter == "OPEN":
            where_clause = "WHERE status = 'OPEN'"
        elif self.current_filter == "CLOSED":
            where_clause = "WHERE status = 'CLOSED'"

        query = (
            "SELECT * FROM positions "
            + where_clause
            + " ORDER BY open_time DESC, id DESC"
        )

        try:
            cur = self.conn.execute(query, params)
            rows = cur.fetchall()
        except Exception as e:
            messagebox.showerror("DB ERROR", f"positions 조회 중 오류:\n{e}")
            return

        # 테이블 채우기
        for row in rows:
            vals = (
                row["id"],
                row["code"],
                row["name"],
                row["side"],
                row["qty"],
                row["entry"],
                row["tp"],
                row["sl"],
                row["open_time"],
                row["close_time"],
                row["status"],
                row["exit_price"],
                row["realized_pnl"],
                row["realized_pnl_rate"],
                row["holding_days"],
                row["note"],
            )
            self.tree.insert("", tk.END, values=vals)

        # 상세 영역 초기화
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert(
            tk.END,
            f"[INFO] {len(rows)}개의 레코드가 조회되었습니다. "
            f"(필터: {self.current_filter})\n",
        )

    # -----------------------------
    # 행 선택 시 상세 표시
    # -----------------------------
    def on_row_selected(self, event):
        selected = self.tree.selection()
        if not selected:
            return

        item_id = selected[0]
        vals = self.tree.item(item_id, "values")
        cols = [
            "id",
            "code",
            "name",
            "side",
            "qty",
            "entry",
            "tp",
            "sl",
            "open_time",
            "close_time",
            "status",
            "exit_price",
            "realized_pnl",
            "realized_pnl_rate",
            "holding_days",
            "note",
        ]

        # 상세 텍스트 갱신
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert(tk.END, "선택된 포지션 상세 정보:\n\n")
        for c, v in zip(cols, vals):
            self.detail_text.insert(tk.END, f"{c:15s}: {v}\n")


# ---------------------------------------------------------
# 메인 실행
# ---------------------------------------------------------

if __name__ == "__main__":
    root = tk.Tk()
    app = PosDBViewer(root)
    root.mainloop()
