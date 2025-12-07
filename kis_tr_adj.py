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

    # 시간대 필터링 (9시부터 10시까지)
    minute_data['datetime'] = minute_data.index  # 인덱스를 datetime으로 변환
    minute_data = minute_data.between_time('09:01', '10:00')  # 9시~10시 구간만 필터링

    return minute_data

# 3. ATR 계산 (변동성 계산)
def calculate_atr(df, period=14):
    df['tr'] = df['High'] - df['Low']
    df['tr'] = df[['tr', (df['High'] - df['Close'].shift()).abs(), (df['Low'] - df['Close'].shift()).abs()]].max(axis=1)
    df['atr'] = df['tr'].rolling(window=period).mean()
    return df

# 4. EMA 계산 (이동평균)
def calculate_ema(df, period=14):
    df['ema'] = df['Close'].ewm(span=period, adjust=False).mean()
    return df


# 트렌드 분석 함수
def analyze_trend(df):
    df = calculate_atr(df)
    df = calculate_ema(df)
    
    # 트렌드 분석
    df['trend'] = np.where(df['Close'] > df['ema'], 'up', 'down')
    
    # 트렌드를 기반으로 조정할 수 있는 매수/매도 조건을 추가할 수 있음
    df['signal'] = np.where(df['trend'] == 'up', 'BUY', 'SELL')
    
    return df

# 6. SL/TP 조정 (변동성에 따른 조정)
def adjust_price_based_on_volatility(atr, entry_price, sl, tp):
    volatility_factor = 1.5  # ATR에 대한 변동성 배수 (조정 가능)

    # 변동성이 급등하면 SL을 넓게, TP를 좁게
    if atr > 2 * np.mean(atr):  # ATR이 평균보다 2배 이상 클 경우
        sl_adjusted = entry_price - 2 * atr  # 더 넓은 SL
        tp_adjusted = entry_price + 2 * atr  # 더 넓은 TP
    else:
        sl_adjusted = entry_price - atr * volatility_factor
        tp_adjusted = entry_price + atr * volatility_factor
    
    # 손절가, 익절가가 현재가에 비해 너무 멀거나 가까우면 보정
    if sl_adjusted < entry_price - atr:  # 너무 낮으면 보정
        sl_adjusted = entry_price - atr
    if tp_adjusted > entry_price + atr:  # 너무 높으면 보정
        tp_adjusted = entry_price + atr
    
    return sl_adjusted, tp_adjusted

# 7. 매매 시그널에 대해 SL/TP 조정
def adjust_signals_based_on_trends(signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    adj_signals = []

    for signal in signals:
        symbol = signal['code']
        entry_price = signal['entry']
        tp = signal['tp']
        sl = signal['sl']

        # 1분봉 데이터를 가져와 트렌드 분석 후 ATR 계산
        minute_data = get_minute_data_from_yahoo(symbol)
        minute_df = pd.DataFrame(minute_data)
        #minute_df['datetime'] = pd.to_datetime(minute_df['datetime'])
        #minute_df.set_index('datetime', inplace=True)

        # 트렌드 분석 및 ATR 계산
        minute_df = analyze_trend(minute_df)
        atr = minute_df['atr'].iloc[-1]  # 최신 ATR 값

        # SL, TP 가격 조정
        sl_adjusted, tp_adjusted = adjust_price_based_on_volatility(atr, entry_price, sl, tp)

        # 조정된 시그널 저장
        adj_signal = signal.copy()
        adj_signal['sl'] = sl_adjusted
        adj_signal['tp'] = tp_adjusted

        adj_signals.append(adj_signal)

    return adj_signals


