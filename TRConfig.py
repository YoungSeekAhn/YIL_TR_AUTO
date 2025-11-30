# DSConfig.py
from dataclasses import dataclass, field
from typing import List, Optional

    
@dataclass
class TRConfig:
    name: str = ""       # 종목 이름
    code: str = ""       # 종목 코드 (자동설정)
    
    duration: int = 365 * 2  # 데이터 기간 (일수)
    start_date: str = ""  # 자동 결정 (예: "20220101")
    end_date: str = ""    # 자동 결정 (예: "20231231")
    
    # 데이터 관련 설정
    
    ## 분할 설정    
    #split: SplitConfig = field(default_factory=SplitConfig)
    batch_size: int = 32  # 배치 크기
    # 저장 경로
    selout_dir: str = "./_selec_out"  # 선택된 출력 결과 저장 디렉토리
    getdata_dir: str = "./_getdata" # CSV 수집 데이터 저장 디렉토리
    
    dataset_dir: str = "./_train/_datasets"  # 데이터셋 저장 디렉토리
    model_dir: str = "./_train/_models"  # 모델 저장 디렉토리
    scaler_dir: str = "./_train/_scalers"  # 스케일러 저장 디렉토리
         
    predict_result_dir: str = "./_predict_result"  # 출력 결과 저장 디렉토리
    predict_report_dir: str = "./_predict_report"  # 예측 결과 저장 디렉토리
    price_report_dir: str = "C:/Users/ganys/python_work/YIL_trading/_price_report"  # 분석 결과 저장 디렉토리
    
    env_dir: str = "./kis_trade"  # .env 파일 경로 (자동매매용)


    #test_getdata_dir: str = "./TR_LSTM3/_csvdata/삼성전자(005930)_20250909.csv"

    ## Display Option
    
config = TRConfig()
