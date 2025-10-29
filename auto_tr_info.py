# -*- coding: utf-8 -*-
"""
KIS 자동 트레이딩 시스템 (ULTRA VERSION) - Part 1
───────────────────────────────────────────────
📦 구성요소:
1️⃣ KISClient  — 거래/시세 API, 토큰 자동갱신
2️⃣ AlertManager — 이메일 + 카카오톡 알림 발송
3️⃣ LogManager — 일별 거래 로그 자동 저장
───────────────────────────────────────────────
"""

import os, sys, json, time, smtplib, requests, csv
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from dotenv import load_dotenv

# 타임존
KST = timezone(timedelta(hours=9))

# ──────────────────────────────────────────────
# 💬 Alert Manager (이메일 + 카카오톡)
# ──────────────────────────────────────────────
class AlertManager:
    def __init__(self):
        load_dotenv()
        self.gmail_id = os.getenv("GMAIL_ID")
        self.gmail_pw = os.getenv("GMAIL_APP_PASSWORD")
        self.receiver = os.getenv("ALERT_RECEIVER")
        self.kakao_token = os.getenv("KAKAO_TOKEN")

    # ✉️ 이메일 발송
    def send_email(self, subject: str, body: str):
        try:
            msg = MIMEText(body, _charset="utf-8")
            msg["Subject"] = subject
            msg["From"] = self.gmail_id
            msg["To"] = self.receiver

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                smtp.login(self.gmail_id, self.gmail_pw)
                smtp.sendmail(self.gmail_id, self.receiver, msg.as_string())
            print(f"[ALERT] 이메일 발송 성공: {subject}")
        except Exception as e:
            print(f"[WARN] 이메일 발송 실패: {e}")

    # 💬 카카오톡 나에게 메시지 보내기 (REST)
    def send_kakao(self, message: str):
        try:
            url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
            headers = {"Authorization": f"Bearer {self.kakao_token}"}
            template = {
                "object_type": "text",
                "text": message,
                "link": {"web_url": "https://www.kakaocorp.com"},
                "button_title": "확인"
            }
            r = requests.post(url, headers=headers, data={"template_object": json.dumps(template)})
            if r.status_code == 200:
                print(f"[ALERT] 카카오톡 발송 성공: {message}")
            else:
                print(f"[WARN] 카카오톡 발송 실패: {r.text}")
        except Exception as e:
            print(f"[WARN] 카카오 API 오류: {e}")

    # 🔔 통합 알림
    def notify(self, title: str, msg: str):
        self.send_email(title, msg)
        self.send_kakao(f"{title}\n{msg}")

# ──────────────────────────────────────────────
# 💾 Log Manager (자동 CSV 저장)
# ──────────────────────────────────────────────
class LogManager:
    def __init__(self):
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        today = datetime.now(KST).strftime("%Y%m%d")
        self.log_path = log_dir / f"{today}_trades.csv"
        if not self.log_path.exists():
            with open(self.log_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["Time", "Stock", "Action", "Qty", "Price", "P/L", "Note"])

    def record(self, stock: str, action: str, qty: int, price: float, pl: float, note: str = ""):
        now = datetime.now(KST).strftime("%H:%M:%S")
        with open(self.log_path, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([now, stock, action, qty, price, pl, note])
        print(f"[LOG] {stock} {action} {qty}@{price} P/L={pl:.0f}")

# ──────────────────────────────────────────────
# 🔐 TokenManager (Access Token 자동 갱신)
# ──────────────────────────────────────────────
class TokenManager:
    def __init__(self, base, appkey, appsecret, refresh_interval=45*60):
        self.base, self.appkey, self.appsecret = base, appkey, appsecret
        self.access_token, self.issued_at = None, 0.0
        self.refresh_interval = refresh_interval

    def _new_token(self):
        url = f"{self.base}/oauth2/tokenP"
        data = {"grant_type": "client_credentials",
                "appkey": self.appkey, "appsecret": self.appsecret}
        r = requests.post(url, json=data, timeout=10)
        r.raise_for_status()
        return r.json().get("access_token", "")

    def get(self):
        now = time.time()
        if not self.access_token or now - self.issued_at > self.refresh_interval:
            self.access_token = self._new_token()
            self.issued_at = now
        return self.access_token

# ──────────────────────────────────────────────
# 📈 기본 KIS Client (API 호출용)
# ──────────────────────────────────────────────
class KISClient:
    def __init__(self, base, appkey, appsecret, account, is_paper):
        self.base = base
        self.appkey = appkey
        self.appsecret = appsecret
        self.account = account
        self.is_paper = is_paper
        self.tok = TokenManager(base, appkey, appsecret)
        self.acct8 = account[:8]
        self.prod2 = "01"
        self.tr_buy = "VTTC0802U" if is_paper else "TTTC0802U"
        self.tr_sell = "VTTC0801U" if is_paper else "TTTC0801U"

    def _headers(self, tr_id=None):
        h = {"authorization": f"Bearer {self.tok.get()}",
             "appkey": self.appkey, "appsecret": self.appsecret}
        if tr_id: h["tr_id"] = tr_id
        h["Content-Type"] = "application/json"
        return h

    def get_price(self, code6):
        url = f"{self.base}/uapi/domestic-stock/v1/quotations/inquire-price"
        h = self._headers("FHKST01010100")
        p = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code6}
        r = requests.get(url, headers=h, params=p, timeout=10)
        try:
            return float(r.json()["output"]["stck_prpr"])
        except Exception:
            return 0.0

    def place_order(self, side, code6, qty, price, ord_dvsn="00"):
        """시장가/지정가 주문"""
        url = f"{self.base}/uapi/domestic-stock/v1/trading/order-cash"
        h = self._headers(tr_id=(self.tr_buy if side == "BUY" else self.tr_sell))
        body = {"CANO": self.acct8, "ACNT_PRDT_CD": self.prod2, "PDNO": code6,
                "ORD_DVSN": ord_dvsn, "ORD_QTY": str(int(qty)),
                "ORD_UNPR": "0" if ord_dvsn == "01" else str(int(price))}
        # hashkey
        hkurl = f"{self.base}/uapi/hashkey"
        hkheader = {"appKey": self.appkey, "appSecret": self.appsecret, "content-type": "application/json"}
        try:
            h["hashkey"] = requests.post(hkurl, headers=hkheader, data=json.dumps(body), timeout=10).json().get("HASH", "")
        except Exception:
            h["hashkey"] = ""
        r = requests.post(url, headers=h, data=json.dumps(body), timeout=10)
        return r.json() if r.text else {}
