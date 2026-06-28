#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
嘉義房租 雲端通知（GitHub Actions 版）
─────────────────────────────────────────────
每日定時讀 Firestore 房東資料，用 Gmail 自動寄提醒給房東（筆電關機也收得到）：
  ① 房客租約到期前 45 天
  ② 定期維護到期前 30 天（廚房濾心／冷氣／洗衣機／水塔）
  ③ 每月預存提醒（每月 1 日，稅務備用金哥哥半額）
  ④ 帳單最後應繳日提醒（偶數月 14、19 日，若仍有緩收帳單）

依賴：pip install google-auth requests
Secrets：GMAIL_ACCOUNT / GMAIL_PASSWORD（應用程式密碼）/ FIREBASE_SERVICE_KEY / NOTIFY_TO(可選)
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
    'filter': '廚房濾心', 'ac': '冷氣清洗(3台)',
    'washer': '洗衣機清洗', 'tank': '水塔清洗+水管',
}

LEASE_MILESTONES = {45, 30, 21, 14, 7, 3, 1, 0}
MAINT_MILESTONES = {30, 14, 7, 3, 1, 0}
BILL_DUE_DAYS    = {14, 19}   # 偶數月這幾天提醒未收帳單（最後應繳日 19）

GMAIL_ACCOUNT  = os.environ.get('GMAIL_ACCOUNT', '').strip()
GMAIL_PASSWORD = os.environ.get('GMAIL_PASSWORD', '').strip()
NOTIFY_TO      = os.environ.get('NOTIFY_TO', '').strip() or GMAIL_ACCOUNT


def firestore_decode(v):
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
    m = d.month - 1 + int(n)
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return d.replace(year=y, month=m, day=day)


def load_db():
    import requests
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    key_json = os.environ.get('FIREBASE_SERVICE_KEY', '')
    if not key_json:
        raise RuntimeError('缺少 FIREBASE_SERVICE_KEY')
    creds = service_account.Credentials.from_service_account_info(
        json.loads(key_json), scopes=['https://www.googleapis.com/auth/datastore'])
    creds.refresh(Request())
    url = (f'https://firestore.googleapis.com/v1/projects/{PROJECT_ID}'
           f'/databases/(default)/documents/{DOC_PATH}')
    r = requests.get(url, headers={'Authorization': f'Bearer {creds.token}'}, timeout=30)
    r.raise_for_status()
    return {k: firestore_decode(x) for k, x in r.json().get('fields', {}).items()}


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
    """① 房客租約到期前 45 天"""
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
        body = (f'{RL.get(r, r)}　{name}\n租約到期日：{expiry}（{left}）\n\n{act}\n\n'
                f'—— 嘉義房租雲端通知（到期前 45 天起，於 45/30/21/14/7/3/1 天自動提醒）')
        try:
            send_mail(f'📅 契約到期提醒：{RL.get(r, r)} {name}（{left}）', body); sent += 1
        except Exception as e:
            print(f'  ⚠️ 契約寄送失敗 {r}: {e}')
    return sent


def check_maintenance(db):
    """② 定期維護到期前 30 天"""
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
        lbl = MAINTENANCE_LABELS.get(r.get('type', ''), r.get('type', ''))
        left = '今天到期' if days == 0 else f'還有 {days} 天'
        body = (f'維護項目：{lbl}\n上次維護：{last}（週期 {cycle} 個月）\n'
                f'預計到期：{nxt.strftime("%Y-%m-%d")}（{left}）\n\n請提前安排廠商。\n\n'
                f'—— 嘉義房租雲端通知（到期前 30 天起，於 30/14/7/3/1 天自動提醒）')
        try:
            send_mail(f'🔧 維護到期提醒：{lbl}（{left}）', body); sent += 1
        except Exception as e:
            print(f'  ⚠️ 維護寄送失敗: {e}')
    return sent


def check_reserve(db):
    """③ 每月預存提醒（每月 1 日，稅務備用金哥哥半額）"""
    today = datetime.now()
    if today.day != 1:
        return 0
    tr = db.get('taxRecords') or {}
    house = float((tr.get('houseTax') or {}).get('amount') or 0)
    land  = float((tr.get('landTax')  or {}).get('amount') or 0)
    half = round((house + land) / 12 / 2)
    if half <= 0:
        return 0
    body = (f'本月請預存稅務備用金（哥哥半額）約 NT${half}。\n'
            f'（房屋稅 {round(house)}＋地價稅 {round(land)}，全年 ÷12 ÷2）\n\n'
            f'房屋稅每年 5 月、地價稅每年 11 月各繳一次；每月預存到期不慌。\n\n'
            f'—— 嘉義房租雲端通知（每月 1 日提醒）')
    try:
        send_mail(f'💰 每月預存提醒：稅務備用金約 NT${half}', body); return 1
    except Exception as e:
        print(f'  ⚠️ 預存寄送失敗: {e}'); return 0


def check_bill_deadline(db):
    """④ 帳單最後應繳日提醒（偶數月 14、19 日，若仍有緩收帳單）"""
    today = datetime.now()
    if today.month % 2 != 0 or today.day not in BILL_DUE_DAYS:
        return 0
    pending = [b for b in (db.get('utilBills') or []) if b.get('deferred')]
    if not pending:
        return 0
    lines = '\n'.join(f'・{b.get("period","")} 期（{b.get("dateStart","")}～{b.get("dateEnd","")}）'
                      for b in pending)
    body = (f'下列水電帳單仍有房客未繳（緩收中），最後應繳日 {today.month}/19：\n\n{lines}\n\n'
            f'請提醒房客於 {today.month}/19 前完成郵局轉帳，避免逾期被台電／台水／瓦斯併入下期。\n\n'
            f'—— 嘉義房租雲端通知（偶數月 14、19 日提醒）')
    try:
        send_mail(f'⏰ 帳單最後應繳日 {today.month}/19 將至，仍有未收帳單', body); return 1
    except Exception as e:
        print(f'  ⚠️ 帳單提醒寄送失敗: {e}'); return 0


def main():
    print('▶ 嘉義房租雲端通知 啟動')
    if not (GMAIL_ACCOUNT and GMAIL_PASSWORD):
        raise RuntimeError('缺少 GMAIL_ACCOUNT / GMAIL_PASSWORD')
    db = load_db()
    print(f'  已讀取（房客 {len(db.get("tenants") or {})}、維護 {len(db.get("maintenanceRecords") or [])}、帳單 {len(db.get("utilBills") or [])}）')
    a = check_lease(db)
    b = check_maintenance(db)
    c = check_reserve(db)
    d = check_bill_deadline(db)
    print(f'✔ 完成：契約 {a}、維護 {b}、預存 {c}、帳單最後應繳日 {d} 封')


if __name__ == '__main__':
    main()
