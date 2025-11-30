"""
kis_trading.py
----------------
YIL_AUTO_TRADE 트레이딩 엔진.

기능:
- KIS 계좌/DB 상태를 기반으로
  오늘자 CSV에서 새로 진입할 종목 결정
- 예수금 한도 / 추가매수 금지 / CSV 순서대로 매수
- 실제 KIS 주문 실행 (현재는 '시장가 매수' 예시)
- 체결된 것으로 간주하고 DB에 포지션 기록

※ 주의:
  - 실제 체결 가격/체결 여부는 KIS API 응답 구조에 맞게
    추가 구현/보정이 필요합니다.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict, Any, Optional

import pandas as pd

from kis_functions import KISAPI
import kis_pos_db  # 위에서 만든 모듈


# ============================================================
# 설정 & 주문 계획 구조체
# ============================================================

@dataclass
class TradingConfig:
    """
    기본 전략 파라미터.
    지금은 단순히 CSV 순서 + 예수금 한도 + 추가 매수 금지만 적용.
    """
    min_confidence: float = 0.0   # 필요하면 필터링에 사용
    min_rr: float = 0.0           # 필요하면 필터링에 사용


@dataclass
class OrderPlan:
    code: str
    name: str
    side: str     # "BUY" (현재 기준)
    qty: int
    entry: float  # CSV에서 제안된 매수가 (참고용)
    tp: Optional[float]
    sl: Optional[float]
    reason: str   # "csv_seq_buy" 등


# ============================================================
# TradingEngine
# ============================================================

class TradingEngine:
    def __init__(self, kis: KISAPI, config: Optional[TradingConfig] = None):
        self.kis = kis
        self.config = config or TradingConfig()

    # --------------------------------------------------------
    # CSV 로드
    # --------------------------------------------------------
    def load_signal_csv(self, csv_path: str) -> pd.DataFrame:
        """
        오늘자 매매 CSV를 읽어 DataFrame으로 반환.
        종목코드는 앞자리 0이 중요하므로 문자열로 읽는다.
        """
        df = pd.read_csv(
            csv_path,
            dtype={"종목코드": str},
        )
        return df

    # --------------------------------------------------------
    # 매수 후보 선정 (CSV → OrderPlan 리스트)
    # --------------------------------------------------------
    def plan_new_entries_from_csv(self, csv_path: str) -> List[OrderPlan]:
        """
        CSV 내용을 기준으로:
        - side == "BUY"인 종목만
        - 이미 보유/OPEN 포지션인 종목은 추가 매수 금지
        - 예수금 한도 내에서, CSV 순서대로 매수 계획 생성
        """
        # 1) 계좌 요약 / 예수금
        summary = self.kis.account.get_summary()
        cash = float(summary.get("cash", 0.0) or 0.0)

        # 2) DB 속 OPEN 포지션 종목들
        open_codes = set(kis_pos_db.get_open_codes())

        # (선택) KIS 실제 보유 종목도 merge 할 수 있음
        # 지금은 kis_functions.AccountService.get_positions()가 미구현 상태일 수 있으므로
        # 나중에 필요하면 여기서 추가.
        # ex:
        # held_positions = self.kis.account.get_positions()
        # held_codes = {p["code"] for p in held_positions}
        # held_codes |= open_codes

        held_codes = set(open_codes)

        # 3) CSV 읽기
        df = self.load_signal_csv(csv_path)

        # 컬럼 이름 상수
        ENTRY_COL = "매수가(entry)"
        TP_COL = "익절가(tp)"
        SL_COL = "손절가(sl)"

        order_plans: List[OrderPlan] = []

        for _, row in df.iterrows():
            side_raw = str(row.get("side", "")).upper()
            if side_raw != "BUY":
                continue

            code = str(row["종목코드"]).strip()
            name = str(row["종목명"]).strip()

            # 추가 매수 금지: DB/Open에 이미 존재하면 스킵
            if code in held_codes:
                continue

            # 필터 (필요시 활성화)
            conf = row.get("confidence")
            rr_val = row.get("RR")

            try:
                conf_f = float(conf) if conf not in (None, "",) else 0.0
            except Exception:
                conf_f = 0.0

            try:
                rr_f = float(rr_val) if rr_val not in (None, "",) else 0.0
            except Exception:
                rr_f = 0.0

            if conf_f < self.config.min_confidence:
                # 신뢰도 필터 사용 시
                # continue
                pass

            if rr_f < self.config.min_rr:
                # RR 필터 사용 시
                # continue
                pass

            # 매수가/수량
            try:
                entry = float(row[ENTRY_COL])
            except Exception:
                # 매수가가 비정상이면 스킵
                continue

            try:
                qty = int(row.get("ord_qty", 0))
            except Exception:
                qty = 0

            if qty <= 0:
                continue

            cost = entry * qty

            # 예수금 부족하면 이 종목은 매수 후보에서 제외
            if cost > cash:
                continue

            # TP/SL
            tp = None
            sl = None
            try:
                val = row.get(TP_COL)
                tp = float(val) if val not in (None, "",) else None
            except Exception:
                pass

            try:
                val = row.get(SL_COL)
                sl = float(val) if val not in (None, "",) else None
            except Exception:
                pass

            plan = OrderPlan(
                code=code,
                name=name,
                side="BUY",
                qty=qty,
                entry=entry,
                tp=tp,
                sl=sl,
                reason="csv_seq_buy",
            )
            order_plans.append(plan)

            # 예수금 차감
            cash -= cost

        return order_plans

    # --------------------------------------------------------
    # 주문 실행 + DB 등록
    # --------------------------------------------------------
    def execute_order_plans(self, plans: List[OrderPlan], signal_df: pd.DataFrame) -> None:
        """
        계획된 OrderPlan들을 실제 KIS 계좌에 주문 실행하고,
        체결된 것으로 보고 DB에 OPEN 포지션으로 기록.

        ※ 현재 예시는 '시장가 매수'로 구현.
        ※ 실제 시스템에서는 주문 응답에서 체결 여부/체결가를 확인해서
           entry_price를 더 정확히 반영하는 것이 좋다.
        """
        # signal_df는 rows를 code 기준으로 찾기 위해 사용
        df_by_code = signal_df.set_index("종목코드", drop=False)

        for plan in plans:
            if plan.side != "BUY":
                continue

            # 1) KIS 주문 실행 (시장가 예시)
            try:
                # 여기에서는 시장가 매수 예시 (kis_functions.OrderService.buy_market 사용)
                resp = self.kis.order.buy_market(plan.code, plan.qty)
                # resp 구조에 따라 체결가를 얻을 수 있으면 entry_price로 사용
                # 지금은 CSV 상의 entry를 그대로 entry_price로 사용
                fill_price = plan.entry
            except Exception as e:
                print(f"[ERROR] Buy order failed for {plan.code}: {e}")
                continue

            # 2) CSV row 찾기
            try:
                row = df_by_code.loc[plan.code].to_dict()
            except KeyError:
                # CSV에서 못 찾으면 최소 정보만 구성
                row = {
                    "종목코드": plan.code,
                    "종목명": plan.name,
                    "매수가(entry)": plan.entry,
                    "익절가(tp)": plan.tp,
                    "손절가(sl)": plan.sl,
                    "Score_1w": None,
                    "RR": None,
                    "confidence": None,
                    "권장호라이즌": None,
                    "holding_days": None,
                    "risk_cap_used": None,
                    "valid_until": None,
                    "source_file": "",
                    "요약점수(h1)": None,
                    "요약점수(h2)": None,
                    "요약점수(h3)": None,
                    "warn_bad_RR": None,
                }

            # 3) DB에 OPEN 포지션 기록
            kis_pos_db.add_open_position_from_csv_row(
                row=row,
                qty=plan.qty,
                entry_price=fill_price,
                open_time_iso=datetime.now().isoformat(timespec="seconds"),
            )

            print(
                f"[INFO] BUY executed: {plan.code} {plan.name}, "
                f"qty={plan.qty}, entry={fill_price}"
            )

    # --------------------------------------------------------
    # TP/SL 체크 (골격) - 나중에 루프/스케줄러에서 호출
    # --------------------------------------------------------
    def check_tp_sl_and_close(self) -> None:
        """
        DB상의 OPEN 포지션들에 대해:
        - 현재가 조회
        - TP/SL 도달 시 계좌에 매도 주문 + DB에서 CLOSED로 업데이트

        ※ 이 함수는 '한 번'만 검사하는 함수.
          실제로는 일정 주기로(예: 10초마다) 호출해줘야 한다.
        """
        open_positions = kis_pos_db.get_open_positions()

        for pos in open_positions:
            code = pos.code
            tp = pos.tp
            sl = pos.sl

            # TP/SL 값이 없으면 이 함수에서 관리하지 않음
            if tp is None and sl is None:
                continue

            # 현재가 조회
            try:
                quote = self.kis.market.get_quote(code)
                # quote JSON 구조에 맞게 현재가 필드를 선택해야 함.
                # 예시: current_price = float(quote["output"]["stck_prpr"])
                # 지금은 예시로 0.0 사용 → 실제 구현 시 수정 필수
                current_price = 0.0
            except Exception as e:
                print(f"[ERROR] get_quote failed for {code}: {e}")
                continue

            should_close = False
            reason = ""

            # BUY 기준 TP/SL
            if tp is not None and current_price >= tp:
                should_close = True
                reason = "tp_hit"
            elif sl is not None and current_price <= sl:
                should_close = True
                reason = "sl_hit"

            if not should_close:
                continue

            # 매도 주문 (시장가 예시)
            try:
                self.kis.order.sell_market(code, pos.qty)
                exit_price = current_price  # 실제로는 체결가 사용
            except Exception as e:
                print(f"[ERROR] Sell order failed for {code}: {e}")
                continue

            # DB 업데이트
            kis_pos_db.close_positions_for_code(
                code=code,
                exit_price=exit_price,
                exit_time_iso=datetime.now().isoformat(timespec="seconds"),
                reason=reason,
            )

            print(
                f"[INFO] CLOSED {code} {pos.name}, "
                f"qty={pos.qty}, exit={exit_price}, reason={reason}"
            )


# ============================================================
# 예시 main (아침 한 번 실행하는 용도)
# ============================================================

if __name__ == "__main__":
    """
    간단 예시 흐름:

    1) DB 초기화
    2) KIS 연결
    3) 오늘자 CSV에서 매수 후보 선정
    4) 주문 실행 + DB에 포지션 등록
    """

    import sys

    if len(sys.argv) < 2:
        print("Usage: python kis_trading.py <today_signals.csv>")
        sys.exit(1)

    csv_path = sys.argv[1]

    # 1) DB 초기화
    kis_pos_db.init_db()

    # 2) KIS API 연결 (환경변수 기반)
    kis = KISAPI.from_env()

    # 3) 엔진 생성
    engine = TradingEngine(kis)

    # 4) 매수 후보 계획
    signal_df = engine.load_signal_csv(csv_path)
    plans = engine.plan_new_entries_from_csv(csv_path)

    print(f"[INFO] Planned {len(plans)} BUY orders.")
    for p in plans:
        print(f" - {p.code} {p.name}, qty={p.qty}, entry={p.entry}")

    # 5) 실제 주문 + DB 반영
    engine.execute_order_plans(plans, signal_df)

    print("[INFO] Morning batch done.")
    print("※ TP/SL 관리를 위해서는 engine.check_tp_sl_and_close()를 "
          "주기적으로 호출하는 루프/스케줄러를 별도로 구현해야 합니다.")
