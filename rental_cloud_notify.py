#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
嘉義房租 雲端通知（GitHub Actions 版）
─────────────────────────────────────────────
功能：每日定時讀取 Firestore 房東資料，檢查「房客租約到期前 45 天」，
      用 Gmail 自動寄提醒信給房東 —— 筆電關機、沒開網頁也能收到。

依賴：pip install google-auth requests
Secrets（GitHub → Settings → Secrets and variables → Actions）：
  GMAIL_ACCOUNT        寄件 Gmail（如 shchyu61@gmail.com）
  GMAIL_PASSWORD       Gmail「應用程式密碼」（非登入密碼）
  FIREBASE_SERVICE_KEY 服務帳戶金鑰 JSON 全文（Firebase 主控台→專案設定→服務帳戶→產生新私密金鑰）
  NOTIFY_TO（可選）    收件信箱，預設同 GMAIL_ACCOUNT
"""
import os, json, smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.header import Header

PROJECT_ID = 'kj-wealth-manager'
APP_ID     = 'kj-rental'
DOC_PATH   = f'artifacts/{APP_ID}/landlord/data'   # Firestore 文件路徑

ROOMS = ['3F前','3F後','4F前','4F後','5F前']
RL = {'3F前':'3樓前','3F後':'3樓後','4F前':'4樓前','4F後':'4樓後','5F前':'5樓前'}

# 寄送里程碑（到期前剩餘天數命中才寄，避免每天重複轟炸；無需寫回資料庫）
LEASE_MILESTONES = {45, 30, 21, 14, 7, 3, 1, 0}

GMAIL_ACCOUNT  = os.environ.get('GMAIL_ACCOUNT', '').strip()
GMAIL_PASSWORD = os.environ.get('GMAIL_PASSWORD', '').strip()
NOTIFY_TO      = os.environ.get('NOTIFY_TO', '').strip() or GMAIL_ACCOUNT


def firestore_decode(v):
    """把 Firestore 型別 JSON 解成一般 Python 值"""
    if 'stringValue'  in v: return v['stringValue']
    if 'integerValue' in v: return int(v['integerValue'])
    if 'doubleValue'  in v: return v['doubleValue']
    if 'booleanValue' in v: return v['booleanValue']
    if 'nullValue'    in v: return None
    if 'timestampValue' in v: return v['timestampValue']
    if 'mapValue'   in v: return {k: firestore_decode(x) for k, x in v['mapValue'].get('fields', {}).items()}
    if 'arrayValue' in v: return [firestore_decode(x) for x in v['arrayValue'].get('values', [])]
    return None


def load_db():
    """以服務帳戶讀取 Firestore 文件，回傳房東 DB dict"""
    import requests
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request

    key_json = os.environ.get('FIREBASE_SERVICE_KEY', '')
    if not key_json:
        raise RuntimeError('缺少 FIREBASE_SERVICE_KEY')
    info = json.loads(key_json)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=['https://www.googleapis.com/auth/datastore'])
    creds.refresh(Request())

    url = (f'https://firestore.googleapis.com/v1/projects/{PROJECT_ID}'
           f'/databases/(default)/documents/{DOC_PATH}')
    r = requests.get(url, headers={'Authorization': f'Bearer {creds.token}'}, timeout=30)
    r.raise_for_status()
    fields = r.json().get('fields', {})
    return {k: firestore_decode(x) for k, x in fields.items()}


def send_mail(subject, body):
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = Header(subject, 'utf-8')
    msg['From'] = GMAIL_ACCOUNT
    msg['To']   = NOTIFY_TO
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=30) as s:
        s.login(GMAIL_ACCOUNT, GMAIL_PASSWORD)
        s.sendmail(GMAIL_ACCOUNT, [NOTIFY_TO], msg.as_string())
    print(f'  ✅ 已寄出：{subject}')


def check_lease(db):
    """檢查租約到期前 45 天（里程碑命中才寄）"""
    tenants = db.get('tenants') or {}
    today = datetime.now()
    sent = 0
    for r in ROOMS:
        t = tenants.get(r)
        if not t or t.get('status') == 'vacant':
            continue
        name = (t.get('name') or '').strip()
        expiry = (t.get('expiry') or '').strip()
        if not name or not expiry:
            continue
        try:
            exp = datetime.strptime(expiry[:10], '%Y-%m-%d')
        except ValueError:
            continue
        days = (exp.date() - today.date()).days
        if days not in LEASE_MILESTONES:
            continue
        intent = t.get('renewalIntent')
        act = ('房客已表達續約意願，請準備續約契約。' if intent == 'yes'
               else '房客已表達不續約，請準備退租點交與押金退還。' if intent == 'no'
               else '房客續約意願未定，請及早確認並安排續約或退租點交。')
        left = '今天到期' if days == 0 else f'還有 {days} 天'
        subject = f'📅 契約到期提醒：{RL.get(r, r)} {name}（{left}）'
        body = (f'{RL.get(r, r)}　{name}\n'
                f'租約到期日：{expiry}（{left}）\n\n'
                f'{act}\n\n'
                f'—— 嘉義房租雲端通知（到期前 45 天起，於 45/30/21/14/7/3/1 天自動提醒）')
        try:
            send_mail(subject, body)
            sent += 1
        except Exception as e:
            print(f'  ⚠️ 寄送失敗 {r}: {e}')
    return sent


def main():
    print('▶ 嘉義房租雲端通知 啟動')
    if not (GMAIL_ACCOUNT and GMAIL_PASSWORD):
        raise RuntimeError('缺少 GMAIL_ACCOUNT / GMAIL_PASSWORD')
    db = load_db()
    print(f'  已讀取房東資料（房客數 {len(db.get("tenants") or {})}）')
    n = check_lease(db)
    print(f'✔ 完成，本次寄出 {n} 封契約提醒')
    # 後續可擴充：維護到期、帳單最後應繳日、每月預存提醒（需另移植 next 計算邏輯）


if __name__ == '__main__':
    main()
