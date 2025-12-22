"""
kis_functions.py
KIS API Wrapper (접속 / 잔고 / 매매 / 시세) - 테스트 및 확장용

구조:
 ① Config & Constants (KISConfig)
 ② Core HTTP Client (KISClient: 토큰 + 공통 request)
 ③ Feature Services
    - AccountService (잔고/예수금/보유종목)
    - OrderService   (현물 매수/매도)
    - MarketService  (현재가 등)
 ④ Facade: KISAPI (외부에서는 이 클래스만 import)
"""

import os
import time
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, List
from datetime import datetime, timedelta
from pykrx import stock

import requests


def is_trading_day(yyyymmdd: str, ticker: str = "005930") -> bool:
    """해당 날짜가 거래일인지 여부 반환 (티커 일봉 데이터 존재 여부로 판단)."""
    df = stock.get_market_ohlcv_by_date(yyyymmdd, yyyymmdd, ticker)
    return df is not None and len(df) > 0

# ──────────────────────────────────────────────
# ✅ 가장 최근 거래일
# ──────────────────────────────────────────────
def last_trading_day(ref: datetime | None = None) -> str:
    """
    기준일(ref) 포함하여 가장 최근 거래일 'YYYYMMDD' 반환.
    ref가 None이면 오늘 기준.
    """
    if ref is None:
        ref = datetime.today()

    d = ref
    while True:
        ymd = d.strftime("%Y%m%d")
        if is_trading_day(ymd):
            return ymd
        d -= timedelta(days=1)


# ──────────────────────────────────────────────
# ✔ 리포트 기준일
#    - 월/토/일 → 최근 거래일 (보통 금요일)
#    - 화~금     → 최근 거래일 이전 거래일
# ──────────────────────────────────────────────
def last_report_day(ref: datetime | None = None) -> str:
    if ref is None:
        ref = datetime.today()

    weekday = ref.weekday()   # 월0 ~ 일6
    last_trade = last_trading_day(ref)

    # 월요일(0), 토요일(5), 일요일(6): 최근 거래일 = 금요일
    if weekday in (0, 5, 6):
        return last_trade

    # 화~금: 최근 거래일 하루 전 거래일
    d = datetime.strptime(last_trade, "%Y%m%d") - timedelta(days=1)

    while True:
        ymd = d.strftime("%Y%m%d")
        if is_trading_day(ymd):
            return ymd
        d -= timedelta(days=1)
        

# ============================================================
# ① Config & Constants
# ============================================================
REAL_BASE_URL_DEFAULT = "https://openapi.koreainvestment.com:9443"
VTS_BASE_URL_DEFAULT  = "https://openapivts.koreainvestment.com:29443"

@dataclass
class KISConfig:
    app_key: str
    app_secret: str
    account_no: str              # 예: "12345678-01"
    base_url: str
    virtual: bool

    @classmethod
    def from_env(cls) -> "KISConfig":
        virtual = os.environ.get("KIS_VIRTUAL", "false").lower() == "true"

        if virtual:
            app_key = os.environ.get("KIS_APPKEY_VTS", "")
            app_secret = os.environ.get("KIS_APPSECRET_VTS", "")
            account_no = os.environ.get("KIS_ACCOUNT_VTS", "")
            base_url = os.environ.get("KIS_BASE_URL_VTS", VTS_BASE_URL_DEFAULT)
        else:
            app_key = os.environ.get("KIS_APP_KEY", "")
            app_secret = os.environ.get("KIS_APP_SECRET", "")
            account_no = os.environ.get("KIS_ACCOUNT_NO", "")
            base_url = os.environ.get("KIS_BASE_URL_REAL", REAL_BASE_URL_DEFAULT)

        return cls(
            app_key=app_key,
            app_secret=app_secret,
            account_no=account_no,
            base_url=base_url,
            virtual=virtual,
        )

    @property
    def cano(self) -> str:
        return self.account_no.split("-")[0]

    @property
    def acnt_prdt_cd(self) -> str:
        return self.account_no.split("-")[1]

# ============================================================
# ② Core HTTP Client (Token 관리 + 공통 request)
# ============================================================

class KISClient:
    """
    - Access Token 관리
    - 공통 HTTP Request 처리
    - 나머지 Service(Account, Order, Market)는 이 클래스를 사용
    """

    def __init__(self, config: KISConfig):
        self.config = config
        self._access_token: Optional[str] = None
        self._token_expire_ts: float = 0
        self._lock = threading.Lock()

    # ----------------------
    # Token 관리
    # ----------------------
    def _ensure_token(self):
        """
        토큰이 없거나 만료되었으면 자동으로 재발급
        """
        with self._lock:
            now = time.time()
            if self._access_token is None or now >= self._token_expire_ts:
                self._get_token()

    def _get_token(self):
        """
        KIS 인증 API 호출해서 Access Token 발급
        ※ URL/응답 필드는 KIS 문서 기준으로 최종 확인 필요
        """
        url = f"{self.config.base_url}/oauth2/tokenP"
        headers = {"Content-Type": "application/json"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
        }

        resp = requests.post(url, json=body, headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()

        # 응답 구조는 실제 print(data)로 확인 권장
        access_token = data.get("access_token") or data.get("accessToken")
        if not access_token:
            raise RuntimeError(f"[KIS] 토큰 응답에 access_token 없음: {data}")

        expires_in = int(data.get("expires_in", 3600))
        self._access_token = access_token
        # 만료 1분 전 여유
        self._token_expire_ts = time.time() + max(expires_in - 60, 60)

    # ----------------------
    # 공통 Request Helper
    # ----------------------
    def request(
        self,
        method: str,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        timeout: int = 5,
    ) -> Dict[str, Any]:
        """
        모든 API 호출이 거치는 공통 함수
        - 토큰 자동 붙이기
        - 에러 공통 처리
        """
        self._ensure_token()

        url = f"{self.config.base_url}{path}"

        base_headers = {
            "Content-Type": "application/json",
            "authorization": f"Bearer {self._access_token}",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
        }
        if headers:
            base_headers.update(headers)

        resp = requests.request(
            method=method,
            url=url,
            headers=base_headers,
            params=params,
            json=body,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()


# ============================================================
# ③ Feature Services (조회 / 매매 / 시세)
# ============================================================

class AccountService:
    """
    잔고, 평가손익, 주문내역, 미체결 등 '계좌/잔고 조회' 담당
    """

    def __init__(self, client: KISClient):
        self.client = client

    # ------------ RAW 잔고 조회 ------------

    def get_balance_raw(self) -> Dict[str, Any]:
        """
        예수금 / 평가금액 / 보유종목 등 잔고 전체 Raw JSON
        - 국내주식 잔고조회 (inquire-balance) 엔드포인트 사용
        - 모의/실전 tr_id 분기
        """
        path = "/uapi/domestic-stock/v1/trading/inquire-balance"

        # 모의/실전 TR ID
        tr_id = "VTTC8434R" if self.client.config.virtual else "TTTC8434R"

        headers = {
            "tr_id": tr_id,
        }
        params = {
            "CANO": self.client.config.cano,
            "ACNT_PRDT_CD": self.client.config.acnt_prdt_cd,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "N",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

        return self.client.request("GET", path, headers=headers, params=params)

    # ------------ 계좌 요약 ------------

    def get_summary(self) -> Dict[str, Any]:
        """
        예수금, 평가금액, 손익 등 요약값 반환

        SUMMARY RAW 구조 기준:
        - raw["output2"][0] 에서 주요 값 사용
            dnca_tot_amt        : 예수금
            scts_evlu_amt       : 주식 평가금액
            tot_evlu_amt / nass_amt : 총 자산
            evlu_pfls_smtl_amt  : 주식 전체 평가손익
            asst_icdc_erng_rt   : 자산 증감률
        """
        raw = self.get_balance_raw()
        output2 = raw.get("output2") or []

        if not output2:
            # 조회할 내용이 없음 등
            return {
                "cash": 0.0,
                "stock_value": 0.0,
                "total_asset": 0.0,
                "pl_amount": 0.0,
                "pl_rate": 0.0,
                "raw": raw,
            }

        e = output2[0]

        def _f(key: str) -> float:
            try:
                return float(e.get(key, "0") or "0")
            except Exception:
                return 0.0

        cash = _f("dnca_tot_amt")                 # 예수금
        stock_value = _f("scts_evlu_amt")         # 주식 평가금액
        total_asset = _f("tot_evlu_amt") or _f("nass_amt")
        pl_amount = _f("evlu_pfls_smtl_amt")      # 주식 전체 평가손익
        pl_rate = _f("asst_icdc_erng_rt")         # 자산 증감률

        return {
            "cash": cash,
            "stock_value": stock_value,
            "total_asset": total_asset,
            "pl_amount": pl_amount,
            "pl_rate": pl_rate,
            "raw": raw,
        }

    # ------------ 보유종목 리스트 ------------

    def get_positions(self) -> List[Dict[str, Any]]:
        """
        보유 종목 리스트를 파싱해서 반환.

        SUMMARY RAW 예:
        'output1': [{
          'pdno': '035420',
          'prdt_name': 'NAVER',
          'hldg_qty': '2',
          'ord_psbl_qty': '2',
          'pchs_avg_pric': '244500.0000',
          'prpr': '243000',
          'evlu_amt': '486000',
          'evlu_pfls_amt': '-3000',
          'evlu_pfls_rt': '-0.61',
          ...
        }]

        → 아래와 같은 dict 리스트로 변환:
        {
          "code": "035420",
          "name": "NAVER",
          "qty": 2,
          "ord_psbl_qty": 2,
          "avg_price": 244500.0,
          "last_price": 243000.0,
          "eval_amount": 486000.0,
          "pl_amount": -3000.0,
          "pl_rate": -0.61,
          "raw": {...원본...},
        }
        """
        raw = self.get_balance_raw()
        output1 = raw.get("output1") or []

        # "조회할 내용이 없습니다"만 있는 경우
        msg1 = (raw.get("msg1") or "").strip()
        if "조회할 내용이 없습니다" in msg1 and not output1:
            return []

        positions: List[Dict[str, Any]] = []

        def _to_int(v: Any) -> int:
            try:
                return int(str(v).replace(",", ""))
            except Exception:
                return 0

        def _to_float(v: Any) -> float:
            try:
                return float(str(v).replace(",", ""))
            except Exception:
                return 0.0

        for item in output1:
            qty = _to_int(item.get("hldg_qty", "0"))
            if qty <= 0:
                continue

            code = (item.get("pdno") or "").strip()
            name = (item.get("prdt_name") or "").strip()

            pos = {
                "code": code,
                "name": name,
                "qty": qty,
                "ord_psbl_qty": _to_int(item.get("ord_psbl_qty", "0")),
                "avg_price": _to_float(item.get("pchs_avg_pric", "0")),
                "last_price": _to_float(item.get("prpr", "0")),
                "eval_amount": _to_float(item.get("evlu_amt", "0")),
                "pl_amount": _to_float(item.get("evlu_pfls_amt", "0")),
                "pl_rate": _to_float(item.get("evlu_pfls_rt", "0")),
                "raw": item,
            }
            positions.append(pos)

        return positions

    def get_positions_map(self) -> Dict[str, Dict[str, Any]]:
        """종목코드 -> 포지션 dict"""
        pos = self.get_positions()
        return {p["code"]: p for p in pos}

    def has_position(self, code: str) -> bool:
        """해당 종목 보유 여부"""
        code = code.strip()
        pos_map = self.get_positions_map()
        return code in pos_map

    # ------------ 간단 ping ------------

    def ping(self) -> bool:
        """
        '접속 테스트' 용 간단 함수.
        - 토큰 발급 + 잔고 조회가 예외 없이 성공하면 True
        """
        try:
            _ = self.get_balance_raw()
            return True
        except Exception as e:
            print("[KIS ping 실패]", e)
            return False


class OrderService:
    """
    현물 매수/매도, 취소/정정 등 '주문' 담당
    """

    def __init__(self, client: KISClient):
        self.client = client

    # 공통 주문 헬퍼
    def _order_cash(
        self,
        tr_id: str,
        symbol: str,
        qty: int,
        ord_dvsn: str,
        price: float | int,
    ) -> Dict[str, Any]:
        """
        현금 주문 공통 함수

        - tr_id: KIS 주문 TR ID
            * 매수: TTTC0802U (예시)
            * 매도: TTTC0801U (예시)
        - ord_dvsn:
            * "01" : 시장가
            * "00" : 지정가
        - price:
            * 시장가일 때는 0 또는 "0"
            * 지정가일 때는 호가단위에 맞게 정수 가격
        """
        if ord_dvsn == "01":
            ord_unpr = "0"
        else:
            ord_unpr = str(int(round(price)))

        path = "/uapi/domestic-stock/v1/trading/order-cash"
        headers = {
            "tr_id": tr_id,
        }
        body = {
            "CANO": self.client.config.cano,
            "ACNT_PRDT_CD": self.client.config.acnt_prdt_cd,
            "PDNO": symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(qty),
            "ORD_UNPR": ord_unpr,
        }
        return self.client.request("POST", path, headers=headers, body=body)

    # ---------------- 매수/매도 래퍼 ----------------

    def buy_market(self, symbol: str, qty: int) -> Dict[str, Any]:
        """
        시장가 매수
        """
        return self._order_cash(
            tr_id="TTTC0802U",  # ⚠ 실제 환경에 맞게 조정
            symbol=symbol,
            qty=qty,
            ord_dvsn="01",      # 시장가
            price=0,
        )

    def sell_market(self, symbol: str, qty: int) -> Dict[str, Any]:
        """
        시장가 매도
        """
        return self._order_cash(
            tr_id="TTTC0801U",  # ⚠ 실제 환경에 맞게 조정
            symbol=symbol,
            qty=qty,
            ord_dvsn="01",      # 시장가
            price=0,
        )

    def buy_limit(self, symbol: str, qty: int, price: float) -> Dict[str, Any]:
        """
        지정가 매수 (LIMIT BUY)
        """
        return self._order_cash(
            tr_id="TTTC0802U",  # 보통 현금 매수 TR
            symbol=symbol,
            qty=qty,
            ord_dvsn="00",      # 지정가
            price=price,
        )

    def sell_limit(self, symbol: str, qty: int, price: float) -> Dict[str, Any]:
        """
        지정가 매도 (LIMIT SELL)
        """
        return self._order_cash(
            tr_id="TTTC0801U",  # 보통 현금 매도 TR
            symbol=symbol,
            qty=qty,
            ord_dvsn="00",      # 지정가
            price=price,
        )

class MarketService:
    """
    현재가, 호가, 과거시세(일봉/분봉) 등 '시세/조회' 담당
    """

    def __init__(self, client: KISClient):
        self.client = client

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        """
        [국내주식] 현재가 조회 (inquire-price)
        """
        path = "/uapi/domestic-stock/v1/quotations/inquire-price"
        headers = {
            "tr_id": "FHKST01010100",  # 모의/실전 동일
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
        }
        return self.client.request("GET", path, headers=headers, params=params)


# ============================================================
# ④ Facade: KISAPI (외부에서 이 클래스만 쓰면 됨)
# ============================================================

class KISAPI:
    """
    외부에서는 이 클래스만 import 해서 사용
    self.account, self.order, self.market 로 기능 분리
    """

    def __init__(self, config: KISConfig):
        self.config = config
        self.client = KISClient(config)

        # ✅ Service 클래스들에 client 주입
        self.account = AccountService(self.client)
        self.order = OrderService(self.client)
        self.market = MarketService(self.client)

    @classmethod
    def from_env(cls) -> "KISAPI":
        config = KISConfig.from_env()
        return cls(config)

    def test_connection(self) -> bool:
        """
        간단 접속 테스트
        - 내부적으로 AccountService.ping() 호출
        """
        return self.account.ping()


# ============================================================
# 단독 실행 테스트용
# ============================================================

if __name__ == "__main__":
    kis = KISAPI.from_env()

    print("=== test_connection() ===")
    print("OK?" , kis.test_connection())

    print("\n=== get_summary() ===")
    summary = kis.account.get_summary()
    print(summary)

    print("\n=== get_positions() ===")
    for p in kis.account.get_positions():
        print(p)

    print("\n=== sample quote (035420) ===")
    try:
        q = kis.market.get_quote("035420")
        print(q)
    except Exception as e:
        print("quote error:", e)