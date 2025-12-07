import yfinance as yf
import pandas as pd
from datetime import datetime

def get_minute_data_from_yahoo(symbol: str, start_date: str, end_date: str):
    """
    주어진 날짜에 대해 1분봉 데이터를 가져옵니다.
    """
    stock = yf.Ticker(symbol)
    minute_data = stock.history(period="1d", interval="1m", start=start_date, end=end_date)
    
    # 9시부터 10시까지 데이터 필터링
    minute_data['datetime'] = minute_data.index
    minute_data = minute_data.between_time('09:00', '10:00')
    
    return minute_data

# 1. 매수, SL, TP 값 로딩
signals = load_signals_from_csv("signals.csv")

# 2. 9시부터 10시까지 1분봉 데이터 가져오기
symbol = "005850.KS"  # 삼성전자
start_date = "2023-12-01"
end_date = "2023-12-01"
minute_data = get_minute_data_from_yahoo(symbol, start_date, end_date)

# 3. EMA, ATR 계산
minute_data = calculate_ema(minute_data, period=14)
minute_data = calculate_atr(minute_data, period=14)

# 4. SL/TP 조정
for signal in signals:
    entry_price = signal['entry']
    atr = minute_data['ATR'].iloc[-1]  # 가장 최근 ATR 값
    sl, tp = adjust_sl_tp(entry_price, atr)
    
    # 결과 출력
    print(f"종목: {signal['name']} ({signal['code']})")
    print(f"매수가: {entry_price}")
    print(f"SL: {sl}")
    print(f"TP: {tp}")
    print("------------")