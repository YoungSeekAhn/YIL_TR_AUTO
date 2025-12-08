import pandas as pd
import numpy as np
import requests
from typing import List, Dict, Any
from datetime import datetime, timedelta
import yfinance as yf

# 2. 1분 분봉 데이터 가져오기 (KIS API 또는 예시 데이터)
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
    1분봉 데이터를 가져오기 위한 함수.
    오늘 날짜를 기준으로 9시부터 10시까지의 데이터를 필터링하여 반환합니다.
    """
    # 오늘 날짜 가져오기
    today = datetime.today().strftime('%Y-%m-%d')  # 'YYYY-MM-DD' 형식으로 가져오기
    today = (datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d')
    
        # yfinance 심볼 변환
    yf_symbol = to_yf_symbol(symbol)
    # 주식 데이터를 1분 단위로 조회 (오늘 날짜의 데이터만)
    stock = yf.Ticker(yf_symbol)
    minute_data = stock.history(period="1d", interval="1m", start=today, end=today)

    # 시간대 필터링 (9시부터 9시30분 까지)
    minute_data['datetime'] = minute_data.index  # 인덱스를 datetime으로 변환
    minute_data = minute_data.between_time('09:00', '09:30')  # 9시~10시 구간만 필터링

    return minute_data

# 3. ATR 계산 (변동성 계산)
def calculate_atr(df, period=14):
    df = df.copy()
    df['prev_close'] = df['Close'].shift(1)
    df['tr1'] = df['High'] - df['Low']
    df['tr2'] = (df['High'] - df['prev_close']).abs()
    df['tr3'] = (df['Low'] - df['prev_close']).abs()
    df['TR'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
    df['ATR'] = df['TR'].rolling(window=period).mean()
    return df

# 4. EMA 계산 (이동평균)
def calculate_ema(df, period=20):
    df = df.copy()
    df['EMA'] = df['Close'].ewm(span=period, adjust=False).mean()
    return df

# 5. Entry/SL/TP 조정 함수
def adjust_entry_sl_tp(min_df, csv_entry, csv_sl, csv_tp, atr, sl_mult=1.5, tp_mult=1.5):
    
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
        
    atr_sl = entry - atr * sl_mult
    atr_tp = entry + atr * tp_mult

    # CSV 기본 조건 존중하면서도 ATR 기반으로 보정
    new_sl = min(csv_sl, atr_sl)
    new_tp = max(csv_tp, atr_tp)

    return entry, new_sl, new_tp


# 7. 매매 시그널에 대해 Entry/SL/TP 조정
def adjust_signals_based_on_trends(signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    adj_signals = []

    for signal in signals:
        symbol = signal['code']
        csv_entry = signal['entry']
        csv_tp = signal['tp']
        csv_sl = signal['sl']

        # 1분봉 데이터를 가져와 트렌드 분석 후 ATR 계산
        minute_data = get_minute_data_from_yahoo(symbol)
        minute_df = pd.DataFrame(minute_data)
        minute_df['datetime'] = pd.to_datetime(minute_df['datetime'])
        minute_df.set_index('datetime', inplace=True)

        # SL, TP 가격 조정
        entry_adjusted, sl_adjusted, tp_adjusted = adjust_entry_sl_tp(minute_df, csv_entry, csv_sl, csv_tp)

        # 조정된 시그널 저장
        adj_signal = signal.copy()
        adj_signal['entry'] = entry_adjusted
        adj_signal['sl'] = sl_adjusted
        adj_signal['tp'] = tp_adjusted
        
        adj_signals.append(adj_signal)

    return adj_signals
