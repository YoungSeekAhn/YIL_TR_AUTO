import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

def to_yf_symbol(code: str, market: str = "KS") -> str:
    """
    한국 종목코드를 yfinance 심볼로 변환.
    예: 005930 → 005930.KS
    시장 구분: KS (코스피), KQ (코스닥)
    """
    code = code.strip()
    if not code.endswith(f".{market}"):
        return f"{code}.{market}"
    return code


def get_minute_data_from_yahoo(symbol: str):
    """
    yfinance 1분봉 데이터를 가져오는 함수.
    → 전일 날짜 기준 09:01~10:00 데이터 반환
    """
    # 전일 날짜 설정
    today = datetime.today().strftime('%Y-%m-%d')

    # yfinance 심볼 변환
    yf_symbol = to_yf_symbol(symbol)

    # 데이터 조회
    stock = yf.Ticker(yf_symbol)
    minute_data = stock.history(
        period="1d",
        interval="1m",
        start=today,
        end=today
    )

    if minute_data.empty:
        print(f"[WARN] {symbol} ({yf_symbol}) 분봉 데이터가 없습니다.")
        return pd.DataFrame()

    # datetime 컬럼 추가
    minute_data["datetime"] = minute_data.index

    # 09:01~10:00 사이 데이터만 필터링
    minute_data = minute_data.between_time("09:01", "10:00")

    return minute_data

symbol = "005930"   # 삼성전자
df = get_minute_data_from_yahoo(symbol)

import pandas as pd

def calculate_atr(df, period=14):
    df = df.copy()
    df['prev_close'] = df['Close'].shift(1)
    df['tr1'] = df['High'] - df['Low']
    df['tr2'] = (df['High'] - df['prev_close']).abs()
    df['tr3'] = (df['Low'] - df['prev_close']).abs()
    df['TR'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
    df['ATR'] = df['TR'].rolling(window=period).mean()
    return df

def calculate_ema(df, period=20):
    df = df.copy()
    df['EMA'] = df['Close'].ewm(span=period, adjust=False).mean()
    return df

def adjust_sl_tp(entry, csv_sl, csv_tp, atr, sl_mult=1.5, tp_mult=1.5):
    atr_sl = entry - atr * sl_mult
    atr_tp = entry + atr * tp_mult

    # CSV 기본 조건 존중하면서도 ATR 기반으로 보정
    new_sl = min(csv_sl, atr_sl)
    new_tp = max(csv_tp, atr_tp)

    return new_sl, new_tp

def analyze_entry_sl_tp(df, csv_entry):
    df = calculate_atr(df)
    df = calculate_ema(df)
    
    last = df.iloc[-1]
    close = last['Close']
    ema = last['EMA']
    atr = last['ATR']

    # 1) 추세 확인
    is_up = close > ema

    if not is_up:
        return {
            "use": False,
            "reason": "downtrend",
        }

    # 2) 엔트리 조정: EMA 근처 범위 안에서만 유효
    allowed_band = atr * 0.5  # 0.5*ATR 이내에서만 매수
    if abs(close - csv_entry) > allowed_band:
        entry = close  # 혹은 스킵 정책 선택 가능
    else:
        entry = csv_entry

    # 3) ATR 기반 SL/TP 설정
    sl, tp = adjust_sl_tp(entry, csv_sl, csv_tp, atr)

    return {
        "use": True,
        "trend": "up",
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "atr": atr,
        "ema": ema,
    }

