import pandas as pd
import numpy as np
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
import yfinance as yf
import math

# ============================================================
# KRX 호가단위(틱) 보정 유틸
# ============================================================

def krx_tick_size(price: float) -> int:
    """
    KRX 기본 호가단위(일반 주식 기준의 일반적인 룰).
    (ETF/ETN/ELW/코넥스 등은 별도 룰 가능)
    """
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
        return 100      # ✅ FIX: 10만~50만 => 100원
    if p < 1000000:
        return 500      # ✅ 50만~100만 => 500원
    return 1000         # ✅ 100만 이상 => 1000원


def align_price_to_tick(price: Optional[float], side: str = "BUY") -> Optional[float]:
    """
    호가단위에 맞춰 가격 보정.
    - BUY  : 틱 단위로 내림
    - SELL : 틱 단위로 올림
    """
    if price is None:
        return None
    p = float(price)
    if p <= 0:
        return None

    tick = krx_tick_size(p)
    q = p / tick

    if side.upper() == "BUY":
        adj = math.floor(q) * tick
    else:
        adj = math.ceil(q) * tick

    return float(adj)


# ============================================================
# yfinance 심볼 변환
# ============================================================

def to_yf_symbol(code: str, market: str = "KS") -> str:
    """
    KRX 6자리 코드를 yfinance 심볼로 변환.
    - market: 'KS'(코스피), 'KQ'(코스닥)
    예) '005850' -> '005850.KS'
    """
    code = (code or "").strip()
    if code.endswith(".KS") or code.endswith(".KQ"):
        return code
    return f"{code}.{market}"


# ============================================================
# yfinance 1분봉 데이터
# ============================================================

def get_minute_data_from_yahoo(symbol: str, market: str = "KS") -> pd.DataFrame:
    """
    오늘 기준 09:00 ~ 09:30 1분봉 데이터.
    - yfinance는 start/end + period 같이 쓰면 에러이므로 period만 사용.
    """
    today_str = datetime.today().strftime("%Y-%m-%d")
    yf_symbol = to_yf_symbol(symbol, market=market)

    stock = yf.Ticker(yf_symbol)
    minute_data = stock.history(period="1d", interval="1m")

    if minute_data is None or minute_data.empty:
        print(f"[WARN] yfinance 1분봉 데이터 없음: {yf_symbol}, date={today_str}")
        return pd.DataFrame()

    if not isinstance(minute_data.index, pd.DatetimeIndex):
        minute_data.index = pd.to_datetime(minute_data.index)

    # 09:00 ~ 09:30 필터
    try:
        filtered = minute_data.between_time("09:00", "09:30")
    except Exception as e:
        print(f"[WARN] between_time 실패: {yf_symbol} err={e}")
        return pd.DataFrame()

    return filtered


# ============================================================
# EMA / ATR
# ============================================================

def calculate_ema(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    df = df.copy()
    close_col = "Close" if "Close" in df.columns else "close"
    df["ema"] = df[close_col].ewm(span=period, adjust=False).mean()
    return df


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    df = df.copy()
    high_col = "High" if "High" in df.columns else "high"
    low_col = "Low" if "Low" in df.columns else "low"
    close_col = "Close" if "Close" in df.columns else "close"

    df["prev_close"] = df[close_col].shift(1)

    tr1 = df[high_col] - df[low_col]
    tr2 = (df[high_col] - df["prev_close"]).abs()
    tr3 = (df[low_col] - df["prev_close"]).abs()

    df["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr"] = df["tr"].rolling(window=period, min_periods=1).mean()
    return df


# ============================================================
# Entry/SL/TP 조정 + 호가 보정 포함
# ============================================================

def adjust_entry_sl_tp(
    minute_df: pd.DataFrame,
    csv_entry: float,
    csv_sl: Optional[float],
    csv_tp: Optional[float],
    sl_mult: float = 1.5,
    tp_mult: float = 1.5,
    ema_weight: float = 0.5,
) -> Tuple[float, Optional[float], Optional[float]]:
    """
    (1) EMA+ATR로 entry/sl/tp 조정
    (2) 최종 entry/sl/tp를 KRX 호가단위로 보정해서 반환
    """

    # --- 0) entry 유효성 방어 ---
    if csv_entry is None or float(csv_entry) <= 0:
        raise ValueError(f"csv_entry must be > 0, got: {csv_entry}")

    # --- 1) 분봉데이터 없으면: 호가보정만 하고 즉시 반환 (중요: return!) ---
    if minute_df is None or minute_df.empty:
        entry_tick = align_price_to_tick(float(csv_entry), "BUY")
        if entry_tick is None:
            entry_tick = float(csv_entry)

        sl_tick = align_price_to_tick(float(csv_sl), "SELL") if csv_sl is not None and float(csv_sl) > 0 else None
        tp_tick = align_price_to_tick(float(csv_tp), "SELL") if csv_tp is not None and float(csv_tp) > 0 else None

        # 관계 보정 (BUY 기준)
        tick = krx_tick_size(entry_tick)
        if sl_tick is not None and sl_tick >= entry_tick:
            sl_tick = align_price_to_tick(entry_tick - tick, "SELL") or (entry_tick - tick)
        if tp_tick is not None and tp_tick <= entry_tick:
            tp_tick = align_price_to_tick(entry_tick + tick, "SELL") or (entry_tick + tick)

        return float(entry_tick), sl_tick, tp_tick

    # --- 2) 데이터 있는 경우: EMA/ATR 계산 ---
    df = minute_df.copy()

    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)

    if "ema" not in df.columns:
        df = calculate_ema(df, period=14)
    if "atr" not in df.columns:
        df = calculate_atr(df, period=14)

    last = df.iloc[-1]
    last_ema = float(last["ema"])
    last_atr = float(last["atr"])

    # --- 3) entry 조정 (CSV vs EMA 혼합) ---
    entry_adjusted = ema_weight * last_ema + (1.0 - ema_weight) * float(csv_entry)

    # --- 4) RR 유지 기반(가능할 때만) ---
    rr_csv: Optional[float] = None
    if csv_sl is not None and csv_tp is not None and float(csv_sl) > 0 and float(csv_tp) > 0:
        # BUY 기준으로만 RR 계산
        risk_dist_csv = max(float(csv_entry) - float(csv_sl), 1e-6)
        reward_dist_csv = max(float(csv_tp) - float(csv_entry), 1e-6)
        rr_csv = reward_dist_csv / risk_dist_csv

    # --- 5) ATR 기반 리스크 거리 ---
    risk_dist = max(last_atr * sl_mult, 1e-6)

    # --- 6) SL/TP 계산 ---
    sl_adjusted = entry_adjusted - risk_dist

    if rr_csv is not None:
        reward_dist = rr_csv * risk_dist
        tp_adjusted: Optional[float] = entry_adjusted + reward_dist
    else:
        tp_adjusted = (entry_adjusted + (last_atr * tp_mult)) if (csv_tp is not None) else None

    # --- 7) 이상치 방어: 값이 말도 안 되면 CSV 기반으로 fallback ---
    if entry_adjusted <= 0 or sl_adjusted <= 0 or (tp_adjusted is not None and tp_adjusted <= 0):
        entry_tick = align_price_to_tick(float(csv_entry), "BUY") or float(csv_entry)
        sl_tick = align_price_to_tick(float(csv_sl), "SELL") if csv_sl is not None and float(csv_sl) > 0 else None
        tp_tick = align_price_to_tick(float(csv_tp), "SELL") if csv_tp is not None and float(csv_tp) > 0 else None
        return float(entry_tick), sl_tick, tp_tick

    # --- 8) 최종 호가단위 보정 ---
    entry_tick = align_price_to_tick(entry_adjusted, "BUY") or float(entry_adjusted)
    sl_tick = align_price_to_tick(sl_adjusted, "SELL") or float(sl_adjusted)
    tp_tick = (align_price_to_tick(tp_adjusted, "SELL") if tp_adjusted is not None else None)
    if tp_adjusted is not None and tp_tick is None:
        tp_tick = float(tp_adjusted)

    # --- 9) 논리적 관계 보정 (BUY 기준: SL < ENTRY < TP) ---
    tick = krx_tick_size(entry_tick)

    if sl_tick is not None and sl_tick >= entry_tick:
        sl_tick = align_price_to_tick(entry_tick - tick, "SELL") or (entry_tick - tick)

    if tp_tick is not None and tp_tick <= entry_tick:
        tp_tick = align_price_to_tick(entry_tick + tick, "SELL") or (entry_tick + tick)

    return float(entry_tick), sl_tick, tp_tick


# ============================================================
# signals 전체 조정
# ============================================================

def adjust_signals_based_on_trends(
    signals: List[Dict[str, Any]],
    market: str = "KS",         # 필요 시 KQ로 호출
    sl_mult: float = 1.5,
    tp_mult: float = 1.5,
    ema_weight: float = 0.5,
) -> List[Dict[str, Any]]:
    adj_signals: List[Dict[str, Any]] = []

    for signal in signals:
        code = signal.get("code")
        if not code:
            continue

        csv_entry = float(signal.get("entry") or 0)
        csv_tp = signal.get("tp")
        csv_sl = signal.get("sl")

        # tp/sl 문자열 가능성 방어
        csv_tp = float(csv_tp) if csv_tp is not None and str(csv_tp).strip() != "" else None
        csv_sl = float(csv_sl) if csv_sl is not None and str(csv_sl).strip() != "" else None

        minute_df = get_minute_data_from_yahoo(code, market=market)

        entry_adj, sl_adj, tp_adj = adjust_entry_sl_tp(
            minute_df,
            csv_entry=csv_entry,
            csv_sl=csv_sl,
            csv_tp=csv_tp,
            sl_mult=sl_mult,
            tp_mult=tp_mult,
            ema_weight=ema_weight,
        )

        adj = signal.copy()
        adj["entry"] = entry_adj
        adj["sl"] = sl_adj
        adj["tp"] = tp_adj

        # (선택) 로그용으로 틱/ATR/EMA를 남기고 싶으면 여기서 추가 저장 가능
        adj_signals.append(adj)

    return adj_signals
