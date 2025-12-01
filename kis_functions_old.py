# kis_functions.py
"""
KIS OpenAPI 래퍼 모듈

필수 환경변수 (예시)
- KIS_APP_KEY        : KIS Developers 앱키
- KIS_APP_SECRET     : KIS Developers 앱시크릿
- KIS_CANO           : 계좌번호 앞 8자리 (CANO)
- KIS_ACNT_PRDT_CD   : 계좌상품코드 (보통 "01")
- KIS_USE_PAPER      : "1" 이면 모의투자, 그 외 실전투자 (옵션)

모의/실전 도메인:
- 모의: https://openapivts.koreainvestment.com:29443
- 실전: https://openapi.koreainvestment.com:9443
"""

from __future__ import annotations

import os
import time
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests


# ──────────────────────────────────────────────────────────────
# 예외 정의
# ──────────────────────────────────────────────────────────────

class KISAPIError(Exception):
    """KIS API 호출 중 발생하는 예외"""


# ──────────────────────────────────────────────────────────────
# 설정 객체
# ──────────────────────────────────────────────────────────────

@dataclass
class KISConfig:
    app_key: str
    app_secret: str
    account_no: str           # CANO (8자리)
    account_product_code: str = "01"
    use_paper: bool = True

    # 도메인
    domain_real: str = "https://openapi.koreainvestment.com:9443"
    domain_paper: str = "https://openapivts.koreainvestment.com:29443"

    @property
    def base_url(self) -> str:
        return self.domain_paper if self.use_paper else self.domain_real


# ──────────────────────────────────────────────────────────────
# 메인 클라이언트: 토큰 + 공통 요청
# ──────────────────────────────────────────────────────────────

class KISAPI:
    def __init__(self, config: KISConfig):
        self.config = config
        self.session = requests.Session()

        self._access_token: Optional[str] = None
        self._token_expire_ts: float = 0.0  # epoch seconds

        # 서비스 핸들러
        self.account = AccountService(self)
        self.order = OrderService(self)
        self.market = MarketService(self)

    # ---- 생성자 헬퍼 ----
    @classmethod
    def from_env(cls) -> "KISAPI":
        """환경변수에서 설정을 읽어 KISAPI 인스턴스를 생성"""
        app_key = os.getenv("KIS_APP_KEY")
        app_secret = os.getenv("KIS_APP_SECRET")
        cano = os.getenv("KIS_ACCOUNT_NO")
        prdt_cd = os.getenv("KIS_ACNT_PRDT_CD", "01")
        use_paper = os.getenv("KIS_VIRTUAL", "False") == "True"

        missing = []
        if not app_key:
            missing.append("KIS_APP_KEY")
        if not app_secret:
            missing.append("KIS_APP_SECRET")
        if not cano:
            missing.append("KIS_CANO")

        if missing:
            raise KISAPIError(f"환경변수 설정 필요: {', '.join(missing)}")

        cfg = KISConfig(
            app_key=app_key,
            app_secret=app_secret,
            account_no=cano,
            account_product_code=prdt_cd,
            use_paper=use_paper,
        )
        return cls(cfg)

    # ---- 토큰 관리 ----
    def _ensure_token(self) -> None:
        """Access Token이 없거나 만료되었으면 재발급"""
        now = time.time()
        if self._access_token and now < self._token_expire_ts - 10:
            return  # 아직 유효

        url = f"{self.config.base_url}/oauth2/tokenP"
        headers = {
            "content-type": "application/json; charset=utf-8",
        }
        body = {
            "grant_type": "client_credentials",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
        }
        resp = self.session.post(url, headers=headers, data=json.dumps(body))
        if resp.status_code != 200:
            raise KISAPIError(
                f"토큰 발급 실패 HTTP {resp.status_code}: {resp.text}"
            )

        data = resp.json()
        # 응답 키 이름은 KIS 문서 기준 (access_token, expires_in 등)
        token = data.get("access_token")
        expires_in = int(data.get("expires_in", 0))

        if not token:
            raise KISAPIError(f"토큰 발급 응답 오류: {data}")

        self._access_token = token
        self._token_expire_ts = now + max(expires_in - 10, 60)

    # ---- HashKey (주문용) ----
    def _get_hashkey(self, body: Dict[str, Any]) -> str:
        """보안 요구가 있는 POST(주문 등)에서 hashkey 생성"""
        url = f"{self.config.base_url}/uapi/hashkey"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
        }
        resp = self.session.post(url, headers=headers, data=json.dumps(body))
        if resp.status_code != 200:
            raise KISAPIError(
                f"hashkey 생성 실패 HTTP {resp.status_code}: {resp.text}"
            )
        data = resp.json()
        if "HASH" not in data:
            raise KISAPIError(f"hashkey 응답 오류: {data}")
        return data["HASH"]

    # ---- 공통 요청 함수 ----
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        tr_id: Optional[str] = None,
        need_hashkey: bool = False,
        timeout: float = 5.0,
    ) -> Dict[str, Any]:
        """
        KIS REST 요청 공통 처리
        path: '/uapi/...' 또는 'uapi/...' 형태 모두 허용
        """
        self._ensure_token()

        if path.startswith("/"):
            url = f"{self.config.base_url}{path}"
        else:
            url = f"{self.config.base_url}/{path}"

        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._access_token}",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
            "custtype": "P",  # 개인
        }
        if tr_id:
            headers["tr_id"] = tr_id

        data = None
        if body is not None:
            # 주문 등 hashkey 필요 시
            if need_hashkey:
                hashkey = self._get_hashkey(body)
                headers["hashkey"] = hashkey
            data = json.dumps(body)

        resp = self.session.request(
            method=method.upper(),
            url=url,
            headers=headers,
            params=params,
            data=data,
            timeout=timeout,
        )

        if resp.status_code != 200:
            raise KISAPIError(
                f"HTTP {resp.status_code} 오류: {resp.text}"
            )

        j = resp.json()
        rt_cd = j.get("rt_cd")
        if rt_cd not in (None, "0"):
            # KIS 표준 에러 구조
            msg_cd = j.get("msg_cd", "")
            msg1 = j.get("msg1", "")
            raise KISAPIError(
                f"KIS 오류 rt_cd={rt_cd}, msg_cd={msg_cd}, msg={msg1}"
            )

        return j


# ──────────────────────────────────────────────────────────────
# AccountService : 잔고/예수금/포지션
# ──────────────────────────────────────────────────────────────

class AccountService:
    def __init__(self, client: KISAPI):
        self.client = client

    # ---- 원시 잔고 조회 (output1, output2 그대로) ----
    def get_raw_balance(self) -> Dict[str, Any]:
        """
        [국내주식] 주문/계좌 > 주식잔고조회
        output1: 보유종목 리스트
        output2: 자산 평가 요약 (dnca_tot_amt, nass_amt 등)
        """
        cfg = self.client.config

        # [실전] TTTC8434R, [모의] VTTC8434R
        tr_id = "VTTC8434R" if cfg.use_paper else "TTTC8434R"

        params = {
            "CANO": cfg.account_no,
            "ACNT_PRDT_CD": cfg.account_product_code,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",          # 01:종목별, 02:종합
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

        data = self.client._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            params=params,
            tr_id=tr_id,
        )
        return data

    # ---- 포지션 리스트 ----
    def get_positions(self) -> List[Dict[str, Any]]:
        """
        보유종목을 파싱해서 반환.
        - code, name, qty, avg_price, last_price, eval_amount, pl, pl_rate 등 숫자 변환 포함
        """
        raw = self.get_raw_balance()
        output1 = raw.get("output1", []) or []

        positions: List[Dict[str, Any]] = []
        for item in output1:
            try:
                qty = int(item.get("hldg_qty", "0"))
            except ValueError:
                qty = 0
            if qty <= 0:
                continue

            code = item.get("pdno", "").strip()
            name = item.get("prdt_name", "").strip()
            prpr = item.get("prpr", "0")
            avg = item.get("pchs_avg_pric", "0")
            eval_amt = item.get("evlu_amt", "0")
            pl_amt = item.get("evlu_pfls_amt", "0")
            pl_rt = item.get("evlu_pfls_rt", "0")

            def _to_float(v: Any) -> float:
                try:
                    return float(str(v))
                except Exception:
                    return 0.0

            positions.append(
                {
                    "code": code,
                    "name": name,
                    "qty": qty,
                    "ord_psbl_qty": int(item.get("ord_psbl_qty", "0") or "0"),
                    "avg_price": _to_float(avg),
                    "last_price": _to_float(prpr),
                    "eval_amount": _to_float(eval_amt),
                    "pl_amount": _to_float(pl_amt),
                    "pl_rate": _to_float(pl_rt),
                    "raw": item,
                }
            )

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

    # ---- 계좌 요약 정보 ----
    def get_summary(self) -> Dict[str, Any]:
        """
        output2를 이용한 계좌 요약 정보
        - cash: 예수금(추정) = nass_amt - scts_evlu_amt (+대출금)
        - stock_value: 주식 평가금액
        - total_equity: 총 평가자산
        - pl_amount: 평가손익 합계
        - pl_rate: 자산 증감률
        """
        raw = self.get_raw_balance()
        output2 = raw.get("output2", [])
        if not output2:
            # 보유/자산 없을 때 KIOK0560 등 메시지 나오는 경우
            return {
                "cash": 0.0,
                "stock_value": 0.0,
                "total_equity": 0.0,
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

        nass_amt = _f("nass_amt")           # 순자산
        scts_evlu_amt = _f("scts_evlu_amt") # 주식 평가액
        tot_evlu_amt = _f("tot_evlu_amt")   # 총 평가액
        loan_amt = _f("tot_loan_amt")
        pl_sum = _f("evlu_pfls_smtl_amt")   # 평가손익 합계
        asst_icdc_rt = _f("asst_icdc_erng_rt")  # 자산 증감률

        # 예수금(추정) = 순자산 - 주식평가액 + 대출금
        cash_est = nass_amt - scts_evlu_amt + loan_amt

        return {
            "cash": cash_est,
            "stock_value": scts_evlu_amt,
            "total_equity": tot_evlu_amt,
            "pl_amount": pl_sum,
            "pl_rate": asst_icdc_rt,
            "raw": e,
        }


# ──────────────────────────────────────────────────────────────
# OrderService : 매수/매도 주문
# ──────────────────────────────────────────────────────────────

class OrderService:
    def __init__(self, client: KISAPI):
        self.client = client

    def _get_tr_id_order_cash(self, side: str) -> str:
        """
        국내주식 현금 주문 TR_ID 결정
        - side: 'BUY' 또는 'SELL'
        """
        use_paper = self.client.config.use_paper
        if side.upper() == "BUY":
            return "VTTC0802U" if use_paper else "TTTC0802U"
        elif side.upper() == "SELL":
            return "VTTC0801U" if use_paper else "TTTC0801U"
        else:
            raise ValueError(f"side must be 'BUY' or 'SELL', got {side}")

    def order_cash(
        self,
        *,
        code: str,
        qty: int,
        price: int = 0,
        side: str = "BUY",
        order_type: str = "01",
    ) -> Dict[str, Any]:
        """
        국내주식 현금 주문
        - code: 6자리 종목코드
        - qty: 수량 (정수)
        - price: 주문가 (시장가면 0)
        - side: 'BUY' / 'SELL'
        - order_type:
            "00" 지정가
            "01" 시장가
            "02" 조건부지정가
            ...
        """
        cfg = self.client.config

        if qty <= 0:
            raise ValueError("qty must be > 0")

        tr_id = self._get_tr_id_order_cash(side)
        body = {
            "CANO": cfg.account_no,
            "ACNT_PRDT_CD": cfg.account_product_code,
            "PDNO": code,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(int(qty)),
            "ORD_UNPR": str(int(price)),
            # 나머지 필드는 기본값 (필수 아님)
            "CTAC_TLNO": "",
            "SLL_TYPE": "01",
            "ALGO_NO": "",
        }

        data = self.client._request(
            "POST",
            "/uapi/domestic-stock/v1/trading/order-cash",
            body=body,
            tr_id=tr_id,
            need_hashkey=True,
        )
        return data

    # 편의 함수들
    def buy_limit(self, code: str, qty: int, price: int) -> Dict[str, Any]:
        """지정가 매수"""
        return self.order_cash(
            code=code,
            qty=qty,
            price=price,
            side="BUY",
            order_type="00",
        )

    def buy_market(self, code: str, qty: int) -> Dict[str, Any]:
        """시장가 매수"""
        return self.order_cash(
            code=code,
            qty=qty,
            price=0,
            side="BUY",
            order_type="01",
        )

    def sell_limit(self, code: str, qty: int, price: int) -> Dict[str, Any]:
        """지정가 매도"""
        return self.order_cash(
            code=code,
            qty=qty,
            price=price,
            side="SELL",
            order_type="00",
        )

    def sell_market(self, code: str, qty: int) -> Dict[str, Any]:
        """시장가 매도"""
        return self.order_cash(
            code=code,
            qty=qty,
            price=0,
            side="SELL",
            order_type="01",
        )


# ──────────────────────────────────────────────────────────────
# MarketService : 현재가/시세
# ──────────────────────────────────────────────────────────────

class MarketService:
    def __init__(self, client: KISAPI):
        self.client = client

    def get_quote_raw(self, code: str) -> Dict[str, Any]:
        """
        [국내주식] 기본시세 > 주식현재가 시세
        """
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",  # 주식/ETF/ETN
            "FID_INPUT_ISCD": code,
        }
        data = self.client._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            params=params,
            tr_id="FHKST01010100",
        )
        return data

    def get_price(self, code: str) -> float:
        """현재가 (stck_prpr)만 숫자로 반환"""
        raw = self.get_quote_raw(code)
        output = raw.get("output", {})
        try:
            return float(output.get("stck_prpr", "0") or "0")
        except Exception:
            return 0.0


# ──────────────────────────────────────────────────────────────
# 모듈 단독 테스트용 (python kis_functions.py)
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    kis = KISAPI.from_env()
    print("=== Account Summary ===")
    summary = kis.account.get_summary()
    print(summary)

    print("\n=== Positions ===")
    for p in kis.account.get_positions():
        print(p)

    print("\n=== Sample Quote (005930) ===")
    try:
        price = kis.market.get_price("005930")
        print("005930 current price:", price)
    except KISAPIError as e:
        print("Quote error:", e)
