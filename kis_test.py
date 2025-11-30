"""
kis_test.py
KIS 접속 상태 + 잔고/예수금 간단 확인용 테스트 GUI
"""

import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

from kis_functions import KISAPI 


class KISTestGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("KIS TEST - Connection & Balance")

        # KIS API 초기화 (환경변수 기반)
        self.kis = KISAPI.from_env()

        self._build_ui()

    # -----------------------------------------------------
    # UI 구성
    # -----------------------------------------------------
    def _build_ui(self):
        # 상단: 접속 상태 + 버튼
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=10, pady=10)

        self.status_label = ttk.Label(top, text="상태: 미접속", foreground="gray")
        self.status_label.pack(side="left")

        self.btn_test = ttk.Button(top, text="접속 테스트 + 잔고 조회", command=self.on_test_clicked)
        self.btn_test.pack(side="right")

        # 중간: 요약 정보
        summary = ttk.LabelFrame(self.root, text="요약")
        summary.pack(fill="x", padx=10, pady=5)

        self.var_cash = tk.StringVar(value="예수금: -")
        self.var_eval_amt = tk.StringVar(value="평가금액: -")
        self.var_total_asset = tk.StringVar(value="총자산(추정): -")
        self.var_eval_pl = tk.StringVar(value="평가손익: -")

        ttk.Label(summary, textvariable=self.var_cash).grid(row=0, column=0, sticky="w", padx=5, pady=2)
        ttk.Label(summary, textvariable=self.var_eval_amt).grid(row=0, column=1, sticky="w", padx=5, pady=2)
        ttk.Label(summary, textvariable=self.var_total_asset).grid(row=1, column=0, sticky="w", padx=5, pady=2)
        ttk.Label(summary, textvariable=self.var_eval_pl).grid(row=1, column=1, sticky="w", padx=5, pady=2)

        # 보유종목 테이블
        frame_table = ttk.LabelFrame(self.root, text="보유 종목")
        frame_table.pack(fill="both", expand=True, padx=10, pady=5)

        cols = ("code", "name", "qty", "avg_price", "eval_pl")
        self.tree = ttk.Treeview(frame_table, columns=cols, show="headings", height=8)
        self.tree.heading("code", text="종목코드")
        self.tree.heading("name", text="종목명")
        self.tree.heading("qty", text="수량")
        self.tree.heading("avg_price", text="평단가")
        self.tree.heading("eval_pl", text="평가손익")

        self.tree.column("code", width=80)
        self.tree.column("name", width=130)
        self.tree.column("qty", width=60, anchor="e")
        self.tree.column("avg_price", width=80, anchor="e")
        self.tree.column("eval_pl", width=90, anchor="e")

        self.tree.pack(fill="both", expand=True)

        # 로그창
        frame_log = ttk.LabelFrame(self.root, text="로그 / Raw Summary")
        frame_log.pack(fill="both", expand=True, padx=10, pady=5)

        self.log_area = scrolledtext.ScrolledText(frame_log, height=8)
        self.log_area.pack(fill="both", expand=True)

    # -----------------------------------------------------
    # 버튼 클릭 → 별도 스레드에서 KIS 호출
    # -----------------------------------------------------
    def on_test_clicked(self):
        self.status_label.config(text="상태: 조회중...", foreground="orange")
        self.btn_test.config(state="disabled")
        threading.Thread(target=self._do_test, daemon=True).start()

    # -----------------------------------------------------
    # 실제 KIS 호출 로직 (Worker Thread)
    # -----------------------------------------------------
    def _do_test(self):
        try:
            ok = self.kis.test_connection()
            if not ok:
                raise RuntimeError("KIS ping 실패 (잔고 조회 실패)")

            summary = self.kis.account.get_summary()
            positions = self.kis.account.get_positions()

            self.root.after(0, self._update_gui_success, summary, positions)

        except Exception as e:
            self.root.after(0, self._update_gui_error, e)

    # -----------------------------------------------------
    # 성공 시 GUI 반영
    # -----------------------------------------------------
    def _update_gui_success(self, summary, positions):
        self.status_label.config(text="상태: 연결 정상", foreground="green")
        self.btn_test.config(state="normal")

        cash = summary.get("cash", 0.0)
        eval_amt = summary.get("eval_amount", 0.0)
        total_asset = summary.get("total_asset", 0.0)
        eval_pl = summary.get("eval_pl", 0.0)

        self.var_cash.set(f"예수금: {cash:,.0f}원")
        self.var_eval_amt.set(f"평가금액: {eval_amt:,.0f}원")
        self.var_total_asset.set(f"총자산(추정): {total_asset:,.0f}원")
        self.var_eval_pl.set(f"평가손익: {eval_pl:,.0f}원")

        for row in self.tree.get_children():
            self.tree.delete(row)

        for p in positions:
            self.tree.insert(
                "",
                tk.END,
                values=(
                    p.get("code", ""),
                    p.get("name", ""),
                    f"{p.get('qty', 0):.0f}",
                    f"{p.get('avg_price', 0):,.0f}",
                    f"{p.get('eval_pl', 0):,.0f}",
                ),
            )

        self.log_area.delete("1.0", tk.END)
        self.log_area.insert(tk.END, "[SUMMARY RAW 일부]\n")
        self.log_area.insert(tk.END, str(summary.get("raw", {}))[:2000])

    # -----------------------------------------------------
    # 실패 시 GUI 반영
    # -----------------------------------------------------
    def _update_gui_error(self, err: Exception):
        self.status_label.config(text="상태: 오류 발생", foreground="red")
        self.btn_test.config(state="normal")

        msg = f"KIS 조회 중 오류 발생:\n{err}"
        self.log_area.insert(tk.END, "\n[ERROR]\n" + msg + "\n")
        self.log_area.see(tk.END)
        messagebox.showerror("KIS ERROR", msg)


if __name__ == "__main__":
    root = tk.Tk()
    app = KISTestGUI(root)
    root.mainloop()
