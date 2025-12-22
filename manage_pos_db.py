"""
manage_pos_db.py
positions DB를 GUI로 조회/수정/삭제하기 위한 DB 관리자

기능
- 전체/OPEN/CLOSED 필터 조회 (status 컬럼 존재 시)
- 행 선택 → 특정 컬럼 값 수정(Update)
- 행 선택 → 특정 컬럼 값 삭제(NULL)
- 행 선택 → 행(레코드) 삭제(Delete Row)
- 행 선택 → status OPEN -> CLOSED 변경
- DB 스키마 변경(컬럼 달라도) 자동 감지로 동작

실행:
python manage_pos_db.py
"""

import sqlite3
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from typing import List, Optional, Any

# kis_pos_db가 있으면 DB_PATH를 자동 사용
try:
    import kis_pos_db  # type: ignore
except Exception:
    kis_pos_db = None


# ---------------------------------------------------------
# DB 유틸
# ---------------------------------------------------------

def get_db_path() -> str:
    if kis_pos_db is not None and hasattr(kis_pos_db, "DB_PATH"):
        return str(kis_pos_db.DB_PATH)
    return "kis_positions.db"


def connect_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def get_table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    rows = cur.fetchall()
    return [r[1] for r in rows]  # r[1] == column name


# ---------------------------------------------------------
# GUI
# ---------------------------------------------------------

class PosDBManager:
    TABLE = "positions"

    def __init__(self, root: tk.Tk, db_path: Optional[str] = None):
        self.root = root
        self.root.title("KIS Positions DB Manager")

        self.db_path = db_path or get_db_path()
        self.conn: Optional[sqlite3.Connection] = None

        self.current_filter = "ALL"  # ALL / OPEN / CLOSED

        # 현재 테이블 컬럼
        self.db_cols: List[str] = []
        # Treeview에 보여줄 컬럼(표시용)
        self.view_cols: List[str] = []
        # rowid 숨김키
        self.ROWID_KEY = "__rowid__"

        self._connect()
        self._build_ui()
        self.refresh_table()

    # -----------------------------
    # DB connect
    # -----------------------------
    def _connect(self):
        try:
            self.conn = connect_db(self.db_path)
        except Exception as e:
            self.conn = None
            messagebox.showerror("DB ERROR", f"DB 접속 실패:\n{e}")

    # -----------------------------
    # UI
    # -----------------------------
    def _build_ui(self):
        # Top bar
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=10, pady=6)

        self.lbl_db = ttk.Label(top, text=f"DB: {self.db_path}")
        self.lbl_db.pack(side="left")

        ttk.Button(top, text="새로고침", command=self.refresh_table).pack(side="right", padx=3)
        ttk.Button(top, text="CLOSED만", command=lambda: self._set_filter("CLOSED")).pack(side="right", padx=3)
        ttk.Button(top, text="OPEN만", command=lambda: self._set_filter("OPEN")).pack(side="right", padx=3)
        ttk.Button(top, text="전체", command=lambda: self._set_filter("ALL")).pack(side="right", padx=3)

        # Middle: table
        frame_table = ttk.LabelFrame(self.root, text="Positions")
        frame_table.pack(fill="both", expand=True, padx=10, pady=6)

        self.tree = ttk.Treeview(frame_table, columns=[], show="headings", height=14)
        self.tree.pack(side="left", fill="both", expand=True)

        sb_y = ttk.Scrollbar(frame_table, orient="vertical", command=self.tree.yview)
        sb_y.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb_y.set)

        self.tree.bind("<<TreeviewSelect>>", self.on_row_selected)

        # Bottom: edit panel
        edit = ttk.LabelFrame(self.root, text="선택 행 편집 (필드 선택 후 수정/삭제)")
        edit.pack(fill="x", padx=10, pady=6)

        row1 = ttk.Frame(edit)
        row1.pack(fill="x", padx=8, pady=4)

        ttk.Label(row1, text="선택 rowid:").pack(side="left")
        self.var_rowid = tk.StringVar(value="-")
        ttk.Label(row1, textvariable=self.var_rowid, width=10).pack(side="left", padx=6)

        ttk.Label(row1, text="필드(컬럼):").pack(side="left")
        self.cmb_col = ttk.Combobox(row1, values=[], state="readonly", width=22)
        self.cmb_col.pack(side="left", padx=6)

        ttk.Label(row1, text="새 값:").pack(side="left")
        self.ent_value = ttk.Entry(row1, width=40)
        self.ent_value.pack(side="left", padx=6, fill="x", expand=True)

        row2 = ttk.Frame(edit)
        row2.pack(fill="x", padx=8, pady=4)

        ttk.Button(row2, text="필드 값 수정(Update)", command=self.update_selected_field).pack(side="left", padx=3)
        ttk.Button(row2, text="필드 값 삭제(NULL)", command=self.delete_selected_field_value).pack(side="left", padx=3)
        ttk.Button(row2, text="선택 행 삭제(Delete Row)", command=self.delete_selected_row).pack(side="left", padx=3)
        ttk.Button(row2, text="선택 행 OPEN→CLOSED", command=self.set_selected_open_to_closed).pack(side="left", padx=3)

        # Detail / log
        detail = ttk.LabelFrame(self.root, text="상세 / 로그")
        detail.pack(fill="both", expand=True, padx=10, pady=6)

        self.txt = scrolledtext.ScrolledText(detail, height=8)
        self.txt.pack(fill="both", expand=True)

    # -----------------------------
    # 작은 유틸
    # -----------------------------
    def _log(self, msg: str):
        self.txt.insert(tk.END, msg + "\n")
        self.txt.see(tk.END)

    def _require_conn(self) -> sqlite3.Connection:
        if self.conn is None:
            self._connect()
        if self.conn is None:
            raise RuntimeError("DB 연결이 없습니다.")
        return self.conn

    def _set_filter(self, mode: str):
        self.current_filter = mode
        self.refresh_table()

    def _selected_rowid(self) -> Optional[int]:
        sel = self.tree.selection()
        if not sel:
            return None
        iid = sel[0]
        try:
            return int(iid)
        except Exception:
            return None

    def _has_col(self, col: str) -> bool:
        return col in self.db_cols

    def _row_value(self, row: sqlite3.Row, col: str, default: Any = "") -> Any:
        """sqlite3.Row 안전 접근 (row.get() 없음 문제 해결)"""
        try:
            keys = row.keys()
            if col in keys:
                return row[col]
            return default
        except Exception:
            return default

    # -----------------------------
    # Refresh table
    # -----------------------------
    def refresh_table(self):
        conn = self._require_conn()

        if not table_exists(conn, self.TABLE):
            messagebox.showinfo(
                "정보",
                f"'{self.TABLE}' 테이블이 존재하지 않습니다.\n"
                "kis_pos_db.init_db()를 먼저 실행했는지 확인하세요.",
            )
            return

        # DB 컬럼 로드
        self.db_cols = get_table_columns(conn, self.TABLE)

        # 보여줄 컬럼 순서(선호) + 나머지 컬럼
        preferred = [
            "id", "code", "name", "side", "qty",
            "entry", "tp", "sl",
            "open_time", "close_time", "status",
            "exit_price", "exit_reason",
            "realized_pnl", "realized_pnl_rate",
            "holding_days",
            "score_1w", "rr", "confidence", "horizon", "valid_until",
            "note",
        ]
        ordered = [c for c in preferred if c in self.db_cols]
        tail = [c for c in self.db_cols if c not in ordered]
        self.view_cols = ordered + tail

        # 콤보박스 갱신
        self.cmb_col["values"] = self.view_cols
        if self.view_cols:
            self.cmb_col.set(self.view_cols[0])

        # Tree 컬럼 재구성
        self.tree["columns"] = self.view_cols
        for c in self.view_cols:
            self.tree.heading(c, text=c)
            w = 90
            if c in ("name", "note"):
                w = 160
            if c in ("open_time", "close_time", "valid_until"):
                w = 160
            self.tree.column(c, width=w, anchor="w")

        # 기존 row clear
        for r in self.tree.get_children():
            self.tree.delete(r)

        # WHERE
        where = ""
        if self._has_col("status"):
            if self.current_filter == "OPEN":
                where = "WHERE status = 'OPEN'"
            elif self.current_filter == "CLOSED":
                where = "WHERE status = 'CLOSED'"

        # rowid 포함 select
        query = f"""
            SELECT rowid AS {self.ROWID_KEY}, *
            FROM {self.TABLE}
            {where}
            ORDER BY rowid DESC
        """

        try:
            cur = conn.execute(query)
            rows = cur.fetchall()
        except Exception as e:
            messagebox.showerror("DB ERROR", f"positions 조회 중 오류:\n{e}")
            return

        for row in rows:
            rowid = int(row[self.ROWID_KEY])
            values = tuple(self._row_value(row, c, "") for c in self.view_cols)
            self.tree.insert("", tk.END, iid=str(rowid), values=values)

        self.var_rowid.set("-")
        self.ent_value.delete(0, tk.END)

        self.txt.delete("1.0", tk.END)
        self._log(f"[INFO] rows={len(rows)} / filter={self.current_filter}")
        self._log(f"[INFO] columns={', '.join(self.view_cols)}")

    # -----------------------------
    # Selection -> detail
    # -----------------------------
    def on_row_selected(self, event=None):
        rowid = self._selected_rowid()
        if rowid is None:
            return
        self.var_rowid.set(str(rowid))

        item = self.tree.item(str(rowid))
        vals = item.get("values", [])

        self.txt.delete("1.0", tk.END)
        self._log(f"[SELECT] rowid={rowid}")
        for c, v in zip(self.view_cols, vals):
            self._log(f"  - {c}: {v}")

    # -----------------------------
    # Update field
    # -----------------------------
    def update_selected_field(self):
        conn = self._require_conn()
        rowid = self._selected_rowid()
        if rowid is None:
            messagebox.showwarning("선택 필요", "먼저 수정할 행을 선택하세요.")
            return

        col = self.cmb_col.get().strip()
        if not col:
            messagebox.showwarning("선택 필요", "수정할 필드(컬럼)를 선택하세요.")
            return
        if col not in self.db_cols:
            messagebox.showerror("오류", f"DB에 없는 컬럼입니다: {col}")
            return

        new_value = self.ent_value.get()

        # 위험한 컬럼 수정 방지(원하면 해제 가능)
        if col == "id":
            messagebox.showwarning("제한", "id 컬럼은 수정 불가로 막아두었습니다.")
            return

        if not messagebox.askyesno("확인", f"rowid={rowid} 의 [{col}] 값을\n'{new_value}' 로 수정할까요?"):
            return

        try:
            conn.execute(
                f"UPDATE {self.TABLE} SET {col} = ? WHERE rowid = ?",
                (new_value, rowid),
            )
            conn.commit()
            self._log(f"[OK] UPDATE rowid={rowid} col={col} -> {new_value}")
            self.refresh_table()
        except Exception as e:
            messagebox.showerror("DB ERROR", f"수정 실패:\n{e}")

    # -----------------------------
    # Delete field value (set NULL)
    # -----------------------------
    def delete_selected_field_value(self):
        conn = self._require_conn()
        rowid = self._selected_rowid()
        if rowid is None:
            messagebox.showwarning("선택 필요", "먼저 삭제할 행을 선택하세요.")
            return

        col = self.cmb_col.get().strip()
        if not col:
            messagebox.showwarning("선택 필요", "삭제할 필드(컬럼)를 선택하세요.")
            return
        if col not in self.db_cols:
            messagebox.showerror("오류", f"DB에 없는 컬럼입니다: {col}")
            return

        if col == "id":
            messagebox.showwarning("제한", "id 컬럼은 NULL 처리 불가로 막아두었습니다.")
            return

        if not messagebox.askyesno("확인", f"rowid={rowid} 의 [{col}] 값을 NULL로 삭제할까요?"):
            return

        try:
            conn.execute(
                f"UPDATE {self.TABLE} SET {col} = NULL WHERE rowid = ?",
                (rowid,),
            )
            conn.commit()
            self._log(f"[OK] SET NULL rowid={rowid} col={col}")
            self.refresh_table()
        except Exception as e:
            messagebox.showerror("DB ERROR", f"필드 NULL 실패:\n{e}")

    # -----------------------------
    # Delete row
    # -----------------------------
    def delete_selected_row(self):
        conn = self._require_conn()
        rowid = self._selected_rowid()
        if rowid is None:
            messagebox.showwarning("선택 필요", "먼저 삭제할 행을 선택하세요.")
            return

        if not messagebox.askyesno("확인", f"rowid={rowid} 레코드를 DB에서 완전히 삭제할까요?"):
            return

        try:
            conn.execute(f"DELETE FROM {self.TABLE} WHERE rowid = ?", (rowid,))
            conn.commit()
            self._log(f"[OK] DELETE row rowid={rowid}")
            self.refresh_table()
        except Exception as e:
            messagebox.showerror("DB ERROR", f"행 삭제 실패:\n{e}")

    # -----------------------------
    # Set OPEN -> CLOSED
    # -----------------------------
    def set_selected_open_to_closed(self):
        conn = self._require_conn()
        rowid = self._selected_rowid()
        if rowid is None:
            messagebox.showwarning("선택 필요", "먼저 변경할 행을 선택하세요.")
            return

        if not self._has_col("status"):
            messagebox.showerror("불가", "status 컬럼이 없어 OPEN→CLOSED 변경을 할 수 없습니다.")
            return

        if not messagebox.askyesno("확인", f"rowid={rowid} 의 status를 OPEN→CLOSED로 변경할까요?"):
            return

        try:
            conn.execute(
                f"UPDATE {self.TABLE} SET status = 'CLOSED' WHERE rowid = ?",
                (rowid,),
            )
            conn.commit()
            self._log(f"[OK] status OPEN→CLOSED rowid={rowid}")
            self.refresh_table()
        except Exception as e:
            messagebox.showerror("DB ERROR", f"status 변경 실패:\n{e}")


# ---------------------------------------------------------
# main
# ---------------------------------------------------------

def main():
    root = tk.Tk()
    app = PosDBManager(root)
    root.mainloop()


if __name__ == "__main__":
    main()
