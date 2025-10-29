# ──────────────────────────────────────────────────────────────
# Part 2 — GUI + 그래프 + ProgressBar + 공유상태
# ──────────────────────────────────────────────────────────────
import tkinter as tk
from tkinter import ttk, scrolledtext
from collections import deque
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import threading

# ──────────────────────────────────────────────────────────────
# 공유 상태: 감시 쓰레드 ↔ GUI 간 현재가/상태/손익 교환
# ──────────────────────────────────────────────────────────────
class SharedState:
    def __init__(self):
        self._lock = threading.Lock()
        self.data = {}  # code -> {"price":float,"status":str,"pl":float}
        self.total_pl = 0.0
        self.total_base = 0.0  # 기준 평가액(매수가*수량 합)
        self.equity_curve = deque(maxlen=7200)  # 최대 2시간(1초 주기 가정)

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
        with self._lock:
            self.data = snapshot_dict

    def set_totals(self, total_pl, total_base):
        with self._lock:
            self.total_pl = float(total_pl)
            self.total_base = float(total_base)
            # 누적곡선 업데이트(총 손익만 기록)
            self.equity_curve.append(self.total_pl)

    def snapshot(self):
        with self._lock:
            return (
                {k: dict(v) for k, v in self.data.items()},
                float(self.total_pl),
                float(self.total_base),
                list(self.equity_curve),
            )

# ──────────────────────────────────────────────────────────────
# GUI 본체: 상단(표+요약) / 우측(진행률) / 중앙(그래프) / 하단(로그)
# ──────────────────────────────────────────────────────────────
class TraderGUIUltra:
    """
    - 상단 좌측: 감시/보유 테이블 (현재가/개별 P/L/상태)
    - 상단 우측: TP/SL 진행률 ProgressBar (선택된 종목 기준)
    - 중앙: 실시간 총 손익 그래프 (Equity Curve)
    - 상단 오른쪽: 총 평가손익/수익률 라벨
    - 하단: 실시간 로그
    """
    GUI_REFRESH_MS = 1000

    def __init__(self, df, shared_state, log_queue):
        self.df = df.copy()
        self.shared = shared_state
        self.log_queue = log_queue

        # 테이블 데이터 사전 구조(고정 정보)
        # code -> {"name","qty","entry","tp","sl","rr"}
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
            }

        # Tk 시작
        self.root = tk.Tk()
        self.root.title("KIS Auto Trading Dashboard (ULTRA)")
        self.root.geometry("1280x780")
        self.root.configure(bg="#f4f6f8")

        # 상단 요약 라벨
        self.summary_label = tk.Label(
            self.root, text="총 평가손익: 0원 (0.00%)",
            font=("맑은 고딕", 12, "bold"), bg="#f4f6f8"
        )
        self.summary_label.pack(anchor="e", padx=16, pady=(10, 0))

        # 상단 프레임: 좌(테이블) + 우(진행률)
        top_frame = tk.Frame(self.root, bg="#f4f6f8")
        top_frame.pack(fill="x", padx=10, pady=8)

        # 좌측 테이블
        left = ttk.LabelFrame(top_frame, text="감시/보유 현황", padding=8)
        left.pack(side="left", fill="x", expand=True, padx=(0, 8))

        cols = ("종목코드","종목명","수량","매수가","익절가","손절가","RR","현재가","P/L","상태")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", height=12)
        widths = (90, 180, 70, 90, 90, 90, 60, 90, 100, 90)
        for c, w in zip(cols, widths):
            self.tree.heading(c, text=c)
            self.tree.column(c, anchor="center", width=w)
        self.tree.pack(fill="x")

        # 색상 태그
        self.tree.tag_configure("bought", background="#e7f1ff")
        self.tree.tag_configure("tp_sold", background="#eaffea")
        self.tree.tag_configure("sl_sold", background="#ffecec")
        self.tree.tag_configure("default", background="#ffffff")

        self.row_iid = {}  # code -> iid
        for code, m in self.meta.items():
            iid = self.tree.insert(
                "", "end",
                values=(
                    code, m["name"], m["qty"], fmt0(m["entry"]),
                    fmt0(m["tp"]), fmt0(m["sl"]), m["rr"], "-", "0", "-"
                ),
                tags=("default",)
            )
            self.row_iid[code] = iid

        self.tree.bind("<<TreeviewSelect>>", self._on_select_row)

        # 우측 진행률 박스
        right = ttk.LabelFrame(top_frame, text="목표 진행률 (선택 종목)", padding=12)
        right.pack(side="left", fill="y")

        self.sel_code_var = tk.StringVar(value="-")
        tk.Label(right, text="선택 종목:", font=("맑은 고딕", 10)).pack(anchor="w")
        tk.Label(right, textvariable=self.sel_code_var, font=("맑은 고딕", 12, "bold")).pack(anchor="w", pady=(0, 8))

        self.pb_tp = ttk.Progressbar(right, orient="horizontal", length=260, mode="determinate", maximum=100)
        self.pb_sl = ttk.Progressbar(right, orient="horizontal", length=260, mode="determinate", maximum=100)
        tk.Label(right, text="익절 진행률", font=("맑은 고딕", 10)).pack(anchor="w")
        self.pb_tp.pack(pady=(0, 10))
        tk.Label(right, text="손절 진행률", font=("맑은 고딕", 10)).pack(anchor="w")
        self.pb_sl.pack()

        self.pb_tp_label = tk.Label(right, text="0%", font=("맑은 고딕", 10))
        self.pb_sl_label = tk.Label(right, text="0%", font=("맑은 고딕", 10))
        self.pb_tp_label.pack(pady=(4, 0), anchor="e")
        self.pb_sl_label.pack(pady=(4, 0), anchor="e")

        # 중앙: 실시간 Equity Curve 그래프
        center = ttk.LabelFrame(self.root, text="실시간 총 손익 그래프 (Equity Curve)", padding=8)
        center.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        self.fig = Figure(figsize=(8, 3.6), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("Time (ticks)")
        self.ax.set_ylabel("Total P/L")
        self.ax.grid(True, linewidth=0.4)
        self.line, = self.ax.plot([], [])
        self.canvas = FigureCanvasTkAgg(self.fig, master=center)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        # 하단 로그
        bottom = ttk.LabelFrame(self.root, text="실시간 로그", padding=8)
        bottom.pack(fill="both", expand=False, padx=10, pady=(0,10))

        self.log_box = scrolledtext.ScrolledText(bottom, wrap=tk.WORD, height=10, state="disabled")
        self.log_box.pack(fill="both", expand=True)

        # 첫 행 자동 선택(있다면)
        if self.row_iid:
            first_code = next(iter(self.row_iid.keys()))
            self.tree.selection_set(self.row_iid[first_code])
            self.sel_code_var.set(first_code)

        # 주기 업데이트 예약
        self.root.after(self.GUI_REFRESH_MS, self._tick)

    # 행 선택 시 진행률 바 갱신 기준 종목 변경
    def _on_select_row(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        values = self.tree.item(sel[0], "values")
        code = values[0]
        self.sel_code_var.set(code)

    # 토스트 알림(간단 팝업)
    def toast(self, msg, bg="#4a4a4a"):
        pop = tk.Toplevel(self.root)
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        # 화면 오른쪽 하단 근처
        pop.geometry("+1600+900")
        frame = tk.Frame(pop, bg=bg, padx=12, pady=8)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text=msg, fg="white", bg=bg, font=("맑은 고딕", 10, "bold")).pack()
        self.root.after(2500, pop.destroy)

    # 1초 주기 업데이트: 로그/테이블/진행률/그래프/요약
    def _tick(self):
        self._flush_logs()
        self._refresh_table_and_summary()
        self._refresh_progressbars()
        self._refresh_equity_plot()
        self.root.after(self.GUI_REFRESH_MS, self._tick)

    def _flush_logs(self):
        while not self.log_queue.empty():
            msg = self.log_queue.get()
            self.log_box.config(state="normal")
            self.log_box.insert(tk.END, msg + "\n")
            self.log_box.config(state="disabled")
            self.log_box.yview(tk.END)

    def _refresh_table_and_summary(self):
        snap, total_pl, total_base, _ = self.shared.snapshot()

        # 테이블 갱신 및 색상 태그
        for code, iid in self.row_iid.items():
            meta = self.meta.get(code, {})
            cur_vals = list(self.tree.item(iid, "values"))
            # 현재가/PL/상태
            price = snap.get(code, {}).get("price", cur_vals[7] if len(cur_vals) > 7 else "-")
            pl = snap.get(code, {}).get("pl", 0.0)
            status = snap.get(code, {}).get("status", cur_vals[9] if len(cur_vals) > 9 else "-")

            cur_vals[7] = fmt0(price)
            cur_vals[8] = fmt_comma(pl)
            cur_vals[9] = status
            self.tree.item(iid, values=tuple(cur_vals))

            tag = "default"
            if status == "bought": tag = "bought"
            elif status == "tp_sold": tag = "tp_sold"
            elif status == "sl_sold": tag = "sl_sold"
            self.tree.item(iid, tags=(tag,))

        # 요약(총손익/수익률)
        rate = (total_pl / total_base * 100.0) if total_base > 0 else 0.0
        self.summary_label.config(text=f"총 평가손익: {fmt_comma(total_pl)}원 ({rate:.2f}%)")

    def _refresh_progressbars(self):
        code = self.sel_code_var.get()
        if code not in self.meta:
            self.pb_tp["value"] = 0
            self.pb_sl["value"] = 0
            self.pb_tp_label.config(text="0%")
            self.pb_sl_label.config(text="0%")
            return

        # 기준/목표
        m = self.meta[code]
        entry, tp, sl = float(m["entry"]), float(m["tp"]), float(m["sl"])
        snap, _, _, _ = self.shared.snapshot()
        price = float(snap.get(code, {}).get("price", 0) or 0)

        # 진행률 계산
        tp_pct = progress_toward(entry, tp, price) if tp > 0 and entry > 0 else 0
        sl_pct = progress_toward(entry, sl, price, toward="down") if sl > 0 and entry > 0 else 0
        tp_pct = max(0, min(100, tp_pct))
        sl_pct = max(0, min(100, sl_pct))

        self.pb_tp["value"] = tp_pct
        self.pb_sl["value"] = sl_pct
        self.pb_tp_label.config(text=f"{tp_pct:.0f}%")
        self.pb_sl_label.config(text=f"{sl_pct:.0f}%")

    def _refresh_equity_plot(self):
        _, _, _, eq = self.shared.snapshot()
        self.line.set_data(range(len(eq)), eq)
        # 축 자동 스케일
        if eq:
            ymin = min(eq); ymax = max(eq)
            pad = max(1000, (ymax - ymin) * 0.2)  # 최소 패딩
            self.ax.set_ylim(ymin - pad, ymax + pad)
            self.ax.set_xlim(0, max(100, len(eq)))
        self.canvas.draw_idle()

    def run(self):
        self.root.mainloop()

# ──────────────────────────────────────────────────────────────
# 유틸 함수
# ──────────────────────────────────────────────────────────────
def fmt0(x):
    try:
        f = float(x)
        return f"{f:.0f}"
    except Exception:
        return str(x)

def fmt_comma(x):
    try:
        return f"{float(x):,.0f}"
    except Exception:
        return str(x)

def progress_toward(entry, target, price, toward="up"):
    """
    entry → target 진행률(%). toward="up"은 상승 목표(tp), "down"은 하락 목표(sl)
    """
    entry = float(entry); target = float(target); price = float(price)
    if toward == "up":
        if target <= entry: return 0
        return (price - entry) / (target - entry) * 100.0
    else:
        if target >= entry: return 0
        return (entry - price) / (entry - target) * 100.0
