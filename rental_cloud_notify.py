#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
嘉義房租 雲端通知（GitHub Actions 版）
─────────────────────────────────────────────
功能：每日定時讀 Firestore 房東資料，檢查並用 Gmail 自動寄提醒給房東——
      筆電關機、沒開網頁也能收到。
      ① 房客租約到期前 45 天
      ② 定期維護到期前 30 天（廚房濾心／冷氣／洗衣機／水塔）

依賴：pip install google-auth requests
Secrets：
  GMAIL_ACCOUNT        寄件 Gmail（同時作為收件信箱）
  GMAIL_PASSWORD       Gmail「應用程式密碼」（非登入密碼）
  FIREBASE_SERVICE_KEY 服務帳戶金鑰 JSON 全文
  NOTIFY_TO（可選）    收件信箱，未設則用 GMAIL_ACCOUNT
"""
import os, json, smtplib, calendar
from datetime import datetime
from email.mime.text import MIMEText
from email.header import Header

PROJECT_ID = 'kj-wealth-manager'
APP_ID     = 'kj-rental'
DOC_PATH   = f'artifacts/{APP_ID}/landlord/data'

ROOMS = ['3F前','3F後','4F前','4F後','5F前']
RL = {'3F前':'3樓前','3F後':'3樓後','4F前':'4樓前','4F後':'4樓後','5F前':'5樓前'}

MAINTENANCE_LABELS = {
    'filter': '廚房濾心',
    'ac':     '冷氣清洗(3台)',
    'washer': '洗衣機清洗',
    'tank':   '水塔清洗+水管',
}

# 寄送里程碑（剩餘天數命中才寄，唯讀資料庫、不寫回，避免每天重複轟炸）
LEASE_MILESTONES = {45, 30, 21, 14, 7, 3, 1, 0}
MAINT_MILESTONES = {30, 14, 7, 3, 1, 0}

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


def add_months(d, n):
    """日期加 n 個月（月底自動夾住，例如 1/31+1月→2/28）"""
    m = d.month - 1 + int(n)
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return d.replace(year=y, month=m, day=day)


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
    """① 房客租約到期前 45 天（里程碑命中才寄）"""
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
            print(f'  ⚠️ 契約寄送失敗 {r}: {e}')
    return sent


def check_maintenance(db):
    """② 定期維護到期前 30 天（里程碑命中才寄）"""
    recs = db.get('maintenanceRecords') or []
    today = datetime.now()
    sent = 0
    for r in recs:
        last = (r.get('lastDate') or '').strip()
        if not last:
            continue
        try:
            ld = datetime.strptime(last[:10], '%Y-%m-%d')
        except ValueError:
            continue
        cycle = int(r.get('cycle') or 24)
        nxt = add_months(ld, cycle)
        days = (nxt.date() - today.date()).days
        if days not in MAINT_MILESTONES:
            continue
        rtype = r.get('type', '')
        lbl = MAINTENANCE_LABELS.get(rtype, rtype)
        left = '今天到期' if days == 0 else f'還有 {days} 天'
        subject = f'🔧 維護到期提醒：{lbl}（{left}）'
        body = (f'維護項目：{lbl}\n'
                f'上次維護：{last}（週期 {cycle} 個月）\n'
                f'預計到期：{nxt.strftime("%Y-%m-%d")}（{left}）\n\n'
                f'請提前安排廠商。\n\n'
                f'—— 嘉義房租雲端通知（到期前 30 天起，於 30/14/7/3/1 天自動提醒）')
        try:
            send_mail(subject, body)
            sent += 1
        except Exception as e:
            print(f'  ⚠️ 維護寄送失敗 {rtype}: {e}')
    return sent


def main():
    print('▶ 嘉義房租雲端通知 啟動')
    if not (GMAIL_ACCOUNT and GMAIL_PASSWORD):
        raise RuntimeError('缺少 GMAIL_ACCOUNT / GMAIL_PASSWORD')
    db = load_db()
    print(f'  已讀取房東資料（房客 {len(db.get("tenants") or {})}、維護紀錄 {len(db.get("maintenanceRecords") or [])}）')
    a = check_lease(db)
    b = check_maintenance(db)
    print(f'✔ 完成，本次寄出 契約 {a} 封、維護 {b} 封')
    # 後續可再擴充：帳單最後應繳日、每月預存提醒


if __name__ == '__main__':
    main()
