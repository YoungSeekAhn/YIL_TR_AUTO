# 1. kis_function.py 전체 구조 설계
# 하나의 파일 안을 4계층으로 나누는 구조:
# 1) 설정/공통 상수 영역
# 2) 공통 HTTP 클라이언트 (토큰·요청 담당)
# 3) 기능별 Service 클래스 (조회 / 매매 / 시세 등)
# 4) 최상위 Facade 클래스 (KISAPI) — 외부에 노출되는 단일 인터페이스

# 🔧 계층별 그림
# kis_function.py
#  ├─ ① Config & Constants
#  │    └─ KISConfig (API Key, URL, 계좌번호 등)
#  │
#  ├─ ② Core HTTP Client
#  │    └─ KISClient
#  │          - _get_token()
#  │          - _request()
#  │
#  ├─ ③ Feature Services
#  │    ├─ AccountService   (잔고, 보유종목, 미체결 조회 등)
#  │    ├─ OrderService     (현물 매수/매도, 취소, 정정 등)
#  │    └─ MarketService    (현재가, 호가, 체결, 일봉/분봉 등)
#  │
#  └─ ④ Facade
#       └─ KISAPI
#             - self.account = AccountService(...)
#             - self.order   = OrderService(...)
#             - self.market  = MarketService(...)

"""
kis_functions.py
KIS API Wrapper (접속 / 잔고 / 매매 / 시세) - 테스트 및 확장용
"""

import os
import time
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, List

import requests


# ============================================================
# ① Config & Constants
# ============================================================

@dataclass
class KISConfig:
    app_key: str
    app_secret: str
    account_no: str              # 예: "12345678-01"
    base_url: str                # 예: "https://openapi.koreainvestment.com:9443"
    virtual: bool = False        # 모의투자 여부

    @classmethod
    def from_env(cls) -> "KISConfig":
        """
        환경 변수에서 설정 읽기용 헬퍼
        (실제 환경변수 이름은 프로젝트에 맞게 조정)
        """
        return cls(
            app_key=os.environ.get("KIS_APP_KEY", ""),
            app_secret=os.environ.get("KIS_APP_SECRET", ""),
            account_no=os.environ.get("KIS_ACCOUNT_NO", ""),
            base_url=os.environ.get("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443"),
            virtual=os.environ.get("KIS_VIRTUAL", "false").lower() == "true",
        )

    @property
    def cano(self) -> str:
        """계좌번호 앞 8자리"""
        return self.account_no.split("-")[0]

    @property
    def acnt_prdt_cd(self) -> str:
        """계좌상품코드 (뒷 2자리)"""
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
        ⚠️ URL/응답필드는 문서 기준으로 확인 필요 (일부는 추측입니다)
        """
        url = f"{self.config.base_url}/oauth2/tokenP"  # (추측) 실전/모의에 맞게 수정
        headers = {"Content-Type": "application/json"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
        }

        resp = requests.post(url, json=body, headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()

        # 응답 구조는 실제 print(data)로 반드시 확인
        self._access_token = data.get("access_token") or data.get("accessToken")
        if not self._access_token:
            raise RuntimeError(f"[KIS] 토큰 응답에 access_token 필드를 찾을 수 없음: {data}")

        expires_in = data.get("expires_in", 3600)
        self._token_expire_ts = time.time() + expires_in - 60  # 1분 여유

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

    # ------------ RAW 조회 ------------
    def get_balance_raw(self) -> Dict[str, Any]:
        """
        예수금 / 평가금액 / 보유종목 등 잔고 전체 Raw JSON
        KIS 문서 기준 domestic-stock 잔고조회 API 엔드포인트 사용 (path/tr_id는 예시, 추측입니다)
        """
        path = "/uapi/domestic-stock/v1/trading/inquire-balance"
        headers = {
            "tr_id": "TTTC8434R",  # ⚠️ 추측값, 실제 tr_id 확인 필요
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

    # ------------ 요약/가공 ------------
    def get_summary(self) -> Dict[str, Any]:
        """
        예수금, 평가금액, 손익 등 요약값 반환
        → 안소현 님이 보여준 응답 구조 기준으로 구현
        """
        raw = self.get_balance_raw()

        output1 = raw.get("output1")
        output2 = raw.get("output2")

        summary = {}

        # output1 / output2 중에서 dict 또는 list[0] 사용
        if isinstance(output1, dict) and output1:
            summary = output1
        elif isinstance(output2, dict) and output2:
            summary = output2
        elif isinstance(output1, list) and output1:
            summary = output1[0]
        elif isinstance(output2, list) and output2:
            summary = output2[0]
        else:
            summary = {}

        # ---- 여기부터는 실제로 받은 JSON에 맞춘 필드 ----
        # {'dnca_tot_amt': '2000000', 'scts_evlu_amt': '0',
        #  'tot_evlu_amt': '2000000', 'nass_amt': '2000000',
        #  'asst_icdc_amt': '0', 'asst_icdc_erng_rt': '0.00000000', ...}

        cash = float(summary.get("dnca_tot_amt", 0) or 0)             # 예수금
        stock_eval = float(summary.get("scts_evlu_amt", 0) or 0)      # 주식 평가액
        total_eval = float(
            summary.get("tot_evlu_amt", summary.get("nass_amt", 0)) or 0
        )                                                             # 총자산/평가액
        eval_pl = float(summary.get("asst_icdc_amt", 0) or 0)         # 자산 증감액(손익)

        return {
            "cash": cash,
            "eval_amount": stock_eval,
            "eval_pl": eval_pl,
            "total_asset": total_eval,
            "raw": raw,
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        """
        보유 종목 리스트를 파싱해서 반환.
        현재 응답에서는 '조회할 내용이 없습니다' + 종목 리스트가 없어 빈 리스트.
        이후 실제 보유 종목이 있을 때 JSON 구조를 보고 확장.
        """
        raw = self.get_balance_raw()

        msg1 = (raw.get("msg1") or "").strip()
        if "조회할 내용이 없습니다" in msg1:
            return []

        # ⚠️ 종목이 생기면, 여기서 output1/output2 구조 다시 보고 구현
        return []

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
    → 테스트 GUI에서는 사용 안 하지만 구조만 유지
    """

    def __init__(self, client: KISClient):
        self.client = client

    def buy_market(self, symbol: str, qty: int) -> Dict[str, Any]:
        path = "/uapi/domestic-stock/v1/trading/order-cash"
        headers = {
            "tr_id": "TTTC0802U",  # ⚠️ 추측값
        }
        body = {
            "CANO": self.client.config.cano,
            "ACNT_PRDT_CD": self.client.config.acnt_prdt_cd,
            "PDNO": symbol,
            "ORD_DVSN": "01",   # 시장가
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
        }
        return self.client.request("POST", path, headers=headers, body=body)

    def sell_market(self, symbol: str, qty: int) -> Dict[str, Any]:
        path = "/uapi/domestic-stock/v1/trading/order-cash"
        headers = {
            "tr_id": "TTTC0801U",  # ⚠️ 추측값
        }
        body = {
            "CANO": self.client.config.cano,
            "ACNT_PRDT_CD": self.client.config.acnt_prdt_cd,
            "PDNO": symbol,
            "ORD_DVSN": "01",   # 시장가
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
        }
        return self.client.request("POST", path, headers=headers, body=body)


class MarketService:
    """
    현재가, 호가, 과거시세(일봉/분봉) 등 '시세/조회' 담당
    """

    def __init__(self, client: KISClient):
        self.client = client

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        path = "/uapi/domestic-stock/v1/quotations/inquire-price"
        headers = {
            "tr_id": "FHKST01010100",  # ⚠️ 추측값
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

        # ✅ 여기서 AccountService(client)를 정상적으로 호출
        self.account = AccountService(self.client)
        self.order = OrderService(self.client)
        self.market = MarketService(self.client)

    @classmethod
    def from_env(cls) -> "KISAPI":
        config = KISConfig.from_env()
        return cls(config)

    def test_connection(self) -> bool:
        return self.account.ping()
