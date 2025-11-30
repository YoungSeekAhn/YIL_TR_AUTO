# ──────────────────────────────────────────────────────────────
# YIL_LABs KIS Auto Trading Dashboard (YIL-TR-AUTO)
# - SharedState
# - TraderGUIUltra (계좌/보유현황 + 주문/체결 + 진행률 + 로그)
# ──────────────────────────────────────────────────────────────
import threading
from collections import deque
from datetime import datetime

import tkinter as tk
from tkinter import ttk, scrolledtext

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # (향후 그래프용)
from matplotlib.figure import Figure                               # (향후 그래프용)


# ──────────────────────────────────────────────────────────────
# 공유 상태: 감시 쓰레드 ↔ GUI 간 현재가/상태/손익 교환
# ──────────────────────────────────────────────────────────────
class SharedState:
    def __init__(self):
        self._lock = threading.Lock()
        # code -> {"price": float, "status": str, "pl": float}
        self.data = {}
        self.total_pl = 0.0
        self.total_base = 0.0  # 기준 평가액(매수가*수량 합)
        # 최대 2시간(1초 주기 가정)
        self.equity_curve = deque(maxlen=7200)

    def update_symbol(self, code, *, price=None, status=None, pl=None):
        with self._lock:
            cur = self.data.get(code, {})
            if price is not None:
                cur["price"] = price
            if status is not None:
                cur["status"] = status
            if pl is not None:
                cur["pl"] = pl
            self.data[code] = cur

    def replace_snapshot(self, snapshot_dict):
        """감시 쓰레드 쪽에서 전체 스냅샷을 통째로 교체하고 싶을 때 사용"""
        with self._lock:
            self.data = snapshot_dict

    def set_totals(self, total_pl, total_base):
        with self._lock:
            self.total_pl = float(total_pl)
            self.total_base = float(total_base)
            # 누적곡선 업데이트(총 손익만 기록)
            self.equity_curve.append(self.total_pl)

    def snapshot(self):
        """GUI에서 1초마다 현재 상태를 읽어갈 때 사용"""
        with self._lock:
            return (
                {k: dict(v) for k, v in self.data.items()},
                float(self.total_pl),
                float(self.total_base),
                list(self.equity_curve),
            )


# ────────────────────────────────
# 포맷 함수
# ────────────────────────────────
def fmt0(x):
    try:
        return f"{float(x):.0f}"
    except Exception:
        return str(x)


def fmt_comma(x):
    try:
        return f"{float(x):,.0f}"
    except Exception:
        return str(x)


# ──────────────────────────────────────────────────────────────
# GUI 본체: 상단(시간+총손익) / 계좌·보유 / 주문 / 체결 / 진행률 / 로그
# ──────────────────────────────────────────────────────────────
class TraderGUIUltra:
    """
    - 상단: 현재시각 + 총 평가손익 / 수익률
    - 상단2: 현재 계좌 / 주식보유 현황
    - 중간 상단: 주문 종목 테이블
    - 중간 하단: 체결 종목 테이블 (체결일시 포함)
    - 하단: TP/SL 진행률 + 로그
    - 전체 스크롤 지원 + 창 크기 동기화
    """
    GUI_REFRESH_MS = 1000

    def __init__(self, df, shared_state, log_queue):
        self.df = df.copy()
        self.shared = shared_state
        self.log_queue = log_queue

        # 주문/메타 데이터 (매수가, TP/SL, 수량 등)
        self.meta = {}
        for _, r in self.df.iterrows():
            code = str(r["종목코드"]).zfill(6)
            self.meta[code] = {
                "name": r.get("종목명", ""),
                "qty": int(float(r.get("ord_qty") or 0)),
                "entry": float(r.get("매수가(entry)") or r.get("last_close") or 0),
                "tp": float(r.get("익절가(tp)") or 0),
                "sl": float(r.get("손절가(sl)") or 0),
                "rr": r.get("RR", ""),
                "status": "-",
            }

        # Tk 초기화
        self.root = tk.Tk()
        self.root.title("YIL_LABs KIS Auto Trading Dashboard (YIL-TR-AUTO)")
        self.root.geometry("1280x820")
        self.root.configure(bg="#f4f6f8")

        # ───────────────────────────────
        # 📜 Scrollable Canvas 구조
        # ───────────────────────────────
        self.canvas = tk.Canvas(self.root, bg="#f4f6f8", highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self.root, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = tk.Frame(self.canvas, bg="#f4f6f8")

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        self.window_id = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")

        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        def _resize_frame(event):
            self.canvas.itemconfig(self.window_id, width=event.width)

        self.canvas.bind("<Configure>", _resize_frame)

        # ─────────────── 상단 요약(시간 + 손익) ───────────────
        top_bar = tk.Frame(self.scrollable_frame, bg="#f4f6f8")
        top_bar.pack(fill="x", pady=(10, 0))

        self.time_label = tk.Label(top_bar, text="", font=("맑은 고딕", 11), bg="#f4f6f8")
        self.time_label.pack(side="left", padx=(16, 0))

        self.summary_label = tk.Label(
            top_bar,
            text="총 평가손익: 0원 (0.00%)",
            font=("맑은 고딕", 12, "bold"),
            bg="#f4f6f8",
        )
        self.summary_label.pack(side="right", padx=(0, 16))

        # ─────────────── 계좌 / 주식보유 현황 ───────────────
        account_frame = ttk.LabelFrame(self.scrollable_frame, text="현재 계좌 / 주식보유 현황", padding=8)
        account_frame.pack(fill="x", padx=10, pady=(10, 5))

        self.acct_cols = ("종목코드", "종목명", "보유수량", "현재가", "평가금액", "평가손익", "비고")
        self.acct_tree = ttk.Treeview(account_frame, columns=self.acct_cols, show="headings", height=6)
        for c in self.acct_cols:
            self.acct_tree.heading(c, text=c)
            self.acct_tree.column(c, anchor="center", width=110)
        self.acct_tree.pack(fill="x")

        self.acct_summary = tk.Label(
            account_frame,
            text="총 평가금액: 0원 / 총 손익: 0원",
            font=("맑은 고딕", 10),
            bg="#f4f6f8",
        )
        self.acct_summary.pack(anchor="e", pady=(4, 0))

        # 보유 현황용 행 핸들
        self.acct_rows = {}
        for code, m in self.meta.items():
            iid = self.acct_tree.insert(
                "",
                "end",
                values=(
                    code,
                    m["name"],
                    m["qty"],
                    "0",   # 현재가
                    "0",   # 평가금액
                    "0",   # 평가손익
                    "-",   # 비고
                ),
            )
            self.acct_rows[code] = iid

        # ─────────────── 주문 테이블 ───────────────
        order_frame = ttk.LabelFrame(self.scrollable_frame, text="주문 종목 현황", padding=8)
        order_frame.pack(fill="x", padx=10, pady=(10, 5))

        self.order_cols = ("종목코드", "종목명", "수량", "매수가", "익절가", "손절가", "RR", "상태")
        self.order_tree = ttk.Treeview(order_frame, columns=self.order_cols, show="headings", height=8)
        for c in self.order_cols:
            self.order_tree.heading(c, text=c)
            self.order_tree.column(c, anchor="center", width=100)
        self.order_tree.pack(fill="x")

        self.order_summary = tk.Label(
            order_frame,
            text="주문 총계: 0건 (총 금액: 0원)",
            font=("맑은 고딕", 10),
            bg="#f4f6f8",
        )
        self.order_summary.pack(anchor="e", pady=(4, 0))

        self.order_rows = {}
        for code, m in self.meta.items():
            iid = self.order_tree.insert(
                "",
                "end",
                values=(
                    code,
                    m["name"],
                    m["qty"],
                    fmt0(m["entry"]),
                    fmt0(m["tp"]),
                    fmt0(m["sl"]),
                    m["rr"],
                    m["status"],
                ),
            )
            self.order_rows[code] = iid

        # ─────────────── 체결 테이블 ───────────────
        filled_frame = ttk.LabelFrame(self.scrollable_frame, text="체결 종목 현황", padding=8)
        filled_frame.pack(fill="x", padx=10, pady=(5, 10))

        self.filled_cols = ("종목코드", "종목명", "수량", "매수가", "체결가", "손익", "상태", "체결일시")
        self.filled_tree = ttk.Treeview(filled_frame, columns=self.filled_cols, show="headings", height=8)
        for c in self.filled_cols:
            self.filled_tree.heading(c, text=c)
            self.filled_tree.column(c, anchor="center", width=110)
        self.filled_tree.pack(fill="x")

        self.filled_summary = tk.Label(
            filled_frame,
            text="체결 총계: 0건 (총 손익: 0원)",
            font=("맑은 고딕", 10),
            bg="#f4f6f8",
        )
        self.filled_summary.pack(anchor="e", pady=(4, 0))

        # ─────────────── 진행률 ProgressBar ───────────────
        progress_frame = ttk.LabelFrame(self.scrollable_frame, text="목표 진행률 (선택 종목)", padding=12)
        progress_frame.pack(fill="x", padx=10, pady=(0, 10))

        self.sel_code_var = tk.StringVar(value="-")
        tk.Label(progress_frame, text="선택 종목:", font=("맑은 고딕", 10)).pack(anchor="w")
        tk.Label(progress_frame, textvariable=self.sel_code_var, font=("맑은 고딕", 12, "bold")).pack(
            anchor="w", pady=(0, 8)
        )

        self.pb_tp = ttk.Progressbar(
            progress_frame, orient="horizontal", length=260, mode="determinate", maximum=100
        )
        self.pb_sl = ttk.Progressbar(
            progress_frame, orient="horizontal", length=260, mode="determinate", maximum=100
        )
        tk.Label(progress_frame, text="익절 진행률", font=("맑은 고딕", 10)).pack(anchor="w")
        self.pb_tp.pack(pady=(0, 10))
        tk.Label(progress_frame, text="손절 진행률", font=("맑은 고딕", 10)).pack(anchor="w")
        self.pb_sl.pack()

        self.pb_tp_label = tk.Label(progress_frame, text="0%", font=("맑은 고딕", 10))
        self.pb_sl_label = tk.Label(progress_frame, text="0%", font=("맑은 고딕", 10))
        self.pb_tp_label.pack(pady=(4, 0), anchor="e")
        self.pb_sl_label.pack(pady=(4, 0), anchor="e")

        # ─────────────── 로그창 ───────────────
        bottom = ttk.LabelFrame(self.scrollable_frame, text="실시간 로그", padding=8)
        bottom.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.log_box = scrolledtext.ScrolledText(bottom, wrap=tk.WORD, height=10, state="disabled")
        self.log_box.pack(fill="both", expand=True)

        # 업데이트 루프
        self.root.after(self.GUI_REFRESH_MS, self._tick)
        self.root.bind_all(
            "<MouseWheel>",
            lambda e: self.canvas.yview_scroll(-1 * (e.delta // 120), "units"),
        )

    # ────────────────────────────────
    # 루프
    # ────────────────────────────────
    def _tick(self):
        self._flush_logs()
        self._update_tables()
        self._update_time()
        self.root.after(self.GUI_REFRESH_MS, self._tick)

    def _update_time(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.time_label.config(text=f"🕒 {now}")

    def _flush_logs(self):
        while not self.log_queue.empty():
            msg = self.log_queue.get()
            self.log_box.config(state="normal")
            now = datetime.now().strftime("[%H:%M:%S] ")
            self.log_box.insert(tk.END, now + msg + "\n")
            self.log_box.config(state="disabled")
            self.log_box.yview(tk.END)

    def _update_tables(self):
        snap, total_pl, total_base, _ = self.shared.snapshot()
        order_count = 0
        order_sum = 0
        filled_count = 0
        filled_pl_sum = 0

        # ───────────── 계좌 / 보유 현황 업데이트 ─────────────
        total_eval = 0.0       # 계좌 총 평가금액
        total_pl_acct = 0.0    # 계좌 총 손익(추정)

        for code, m in self.meta.items():
            qty = m.get("qty", 0) or 0
            entry = float(m.get("entry", 0.0) or 0.0)

            sym = snap.get(code, {})
            price = float(sym.get("price", 0.0) or 0.0)
            pl_sym = sym.get("pl", None)

            # per-symbol 손익이 공유 안되면 (price - entry) * qty 로 추정
            if pl_sym is None:
                pl_sym = (price - entry) * qty
            pl_sym = float(pl_sym)

            eval_val = price * qty

            total_eval += eval_val
            total_pl_acct += pl_sym

            iid_acct = self.acct_rows.get(code)
            if iid_acct is not None:
                vals = (
                    code,
                    m["name"],
                    qty,
                    fmt0(price),
                    fmt_comma(eval_val),
                    fmt_comma(pl_sym),
                    "-",  # 비고
                )
                self.acct_tree.item(iid_acct, values=vals)

        self.acct_summary.config(
            text=f"총 평가금액: {fmt_comma(total_eval)}원 / 총 손익: {fmt_comma(total_pl_acct)}원"
        )

        # ───────────── 주문 / 체결 테이블 업데이트 ─────────────
        for code, iid in self.order_rows.items():
            m = self.meta[code]
            price = snap.get(code, {}).get("price", 0)
            status = snap.get(code, {}).get("status", "-")
            pl = snap.get(code, {}).get("pl", 0.0)

            # 체결 처리
            if status in ("tp_sold", "sl_sold"):
                if not any(code == self.filled_tree.set(i, "종목코드") for i in self.filled_tree.get_children()):
                    fill_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self.filled_tree.insert(
                        "",
                        "end",
                        values=(
                            code,
                            m["name"],
                            m["qty"],
                            fmt0(m["entry"]),
                            fmt0(price),
                            fmt_comma(pl),
                            status,
                            fill_time,
                        ),
                    )
                # 요약(이번 틱에서 새로 체결된 것만)
                filled_count += 1
                filled_pl_sum += float(pl or 0.0)
            else:
                order_count += 1
                order_sum += (m["qty"] * m["entry"])

            # 주문 테이블 상태 갱신
            cur_vals = list(self.order_tree.item(iid, "values"))
            cur_vals[-1] = status
            self.order_tree.item(iid, values=tuple(cur_vals))

        self.order_summary.config(
            text=f"주문 총계: {order_count}건 (총 금액: {fmt_comma(order_sum)}원)"
        )
        self.filled_summary.config(
            text=f"체결 총계: {filled_count}건 (총 손익: {fmt_comma(filled_pl_sum)}원)"
        )

        rate = (total_pl / total_base * 100.0) if total_base > 0 else 0.0
        self.summary_label.config(
            text=f"총 평가손익: {fmt_comma(total_pl)}원 ({rate:.2f}%)"
        )

    def run(self):
        self.root.mainloop()

    def toast(self, message, bg=None, duration=3000):
        """
        Display a short, temporary notification window (toast) above the main GUI.
        - message: text to display
        - bg: background color (optional)
        - duration: milliseconds to show
        """
        try:
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            try:
                win.attributes("-topmost", True)
            except Exception:
                pass

            lbl = tk.Label(
                win,
                text=message,
                bg=(bg or "#333333"),
                fg="#ffffff",
                font=("맑은 고딕", 10),
                bd=1,
                relief="solid",
                padx=8,
                pady=4,
            )
            lbl.pack()

            self.root.update_idletasks()
            win.update_idletasks()
            x = self.root.winfo_rootx() + max(
                0, self.root.winfo_width() - win.winfo_reqwidth() - 20
            )
            y = self.root.winfo_rooty() + 20
            win.geometry(f"+{x}+{y}")

            win.after(duration, win.destroy)
        except Exception as e:
            try:
                self.log_queue.put(f"[TOAST ERROR] {e}")
            except Exception:
                pass
