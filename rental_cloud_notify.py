#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
★★★【自我舉證表】rental_cloud_notify(09230021).py（ＡＭ２５）
　關鍵結論｜來源
　・排程改台灣 20:30｜主帥 2026/08/30 13:23「全面改成當天晚上 20:30」（ＡＭ１④）；本檔搭配 rental_notify(09230021).yml
　・重複的 4 種提醒交雲端寄｜主帥 2026/09/23 00:0x 題一 A
　・雲端心跳兩步走｜主帥 2026/09/23 00:0x 題二 A（同意新增心跳）
　・過渡期重複信標示｜主帥 2026/09/23 00:21 第3點
　・公開紀錄不印主旨｜鐵律ＡＪ２（kj-repository 為公開倉庫，Actions 紀錄不必登入即可看）
　・結算日只寄提醒、不自動結算｜主帥 2026/09/23 02:5x 裁示 P3-8 方案 A
　・奇數月結算日 15 日｜主帥 2026/09/21 21:17 第7點原話（對話紀錄檔 2026-09-21-19-24-00）
　・實測｜改版記錄(09230308) 第十一章
★★★【推定清單】
　・推定 Firestore 服務帳號可寫 artifacts/kj-rental/landlord/heartbeat（與 landlord/data 同集合）
　　→ 若為假：心跳寫入失敗，GitHub 紀錄印出警告，網頁 26 小時後紅色警示；寄信與結算不受影響
─────────────────────────────────────────────
嘉義房租 雲端通知（GitHub Actions 版）
─────────────────────────────────────────────
每日定時讀 Firestore 房東資料，用 Gmail 自動寄提醒給房東（筆電關機也收得到）：
  ① 房客租約到期前 45 天
  ② 定期維護到期前 30 天（廚房濾心／冷氣／洗衣機／水塔）
  ③ 每月預存提醒（每月 1 日，稅務備用金哥哥半額）
  ④ 帳單最後應繳日提醒（偶數月 14、19 日，若仍有緩收帳單）
  ⑤ 哥弟結算日提醒（奇數月 15 日、偶數月 20 日 20:30；★只寄提醒、不自動結算，結算一律在網頁完成）
★09230021：排程改每天台灣 20:30（ＡＭ１④）；每次執行寫「雲端心跳」到 landlord/heartbeat（網頁顯示）；
  過渡期（DUP_TRANSITION=True）①～④ 主旨與內文標示「過渡期重複信・正常」。
★09230308：通知三級分類（ＡＭ１⑦：急迫／次日有效／一般）：本程式所有信件皆屬【一般】，只在排程 20:30 寄；
  寄信入口 send_mail 於睡眠時段（台灣 21:30～07:30）攔截非急迫信；睡眠時段手動執行只驗證讀取並寫心跳、不寄信（ＡＭ１①⑥⑦）。
★09230308：⑤ 改為只寄結算日提醒（主帥 2026/09/23 裁示 P3-8 方案 A）：雲端不再自動標記已結算、不寫結算單，避免與網頁三桶重複結算；
  奇數月結算日改 15 日（主帥 2026/09/21 21:17：「網路費帳單13日中華電信公司就寄電子帳單出來,就算再拖個2天緩衝期…算9/15好了」）。

依賴：pip install google-auth requests
Secrets：GMAIL_ACCOUNT / GMAIL_PASSWORD（應用程式密碼）/ FIREBASE_SERVICE_KEY / NOTIFY_TO(可選)
"""
import os, json, smtplib, calendar
from datetime import datetime, timezone, timedelta
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

SCRIPT_VERSION = '09230308'   # 鐵律AA：全檔唯一版本識別處，須＝檔名括號時間戳
HB_PATH = f'artifacts/{APP_ID}/landlord/heartbeat'   # ★雲端心跳：獨立文件，只有本程式寫、網頁只讀
TW = timezone(timedelta(hours=8))
KIND_URGENT, KIND_NEXT_DAY, KIND_NORMAL = '急迫', '次日有效', '一般'   # ★通知三級分類（ＡＭ１⑦）；本程式信件皆為一般


def in_quiet_hours(now=None):
    """睡眠時段＝台灣 21:30～次日 07:30（ＡＭ１①）"""
    t = (now or datetime.now(TW)).astimezone(TW)
    m = t.hour * 60 + t.minute
    return m >= 21 * 60 + 30 or m < 7 * 60 + 30


class QuietSkip(Exception):
    """睡眠時段不寄非急迫信（寄信入口攔截，ＡＭ１⑥⑦）；呼叫端既有 except 會接住，不會被記成已寄"""


DUP_TRANSITION = True   # ★過渡期（網頁與雲端並行）；第二步交付時改 False（改版記錄待辦 P3-19）


def dup_head(src):
    return f'【過渡期重複信・正常｜{src}寄】' if DUP_TRANSITION else ''


def dup_foot(src):
    return ('\n\n──────────\n★這是【過渡期的重複信】，屬正常，不是程式壞掉，不必找 AI 修。\n・原因：2026/09/23 起，雲端通知改為每天 20:30 寄出，並加裝「雲端心跳」（每次執行都在雲端留下紀錄）。依鐵律ＡＭ１２，必須先確認雲端連續 3 天正常，網頁才能停寄，所以驗收期間同一類提醒會收到兩封：一封【網頁寄】、一封【雲端寄】。\n・本信由：【' + src + '】寄出。\n・何時結束：房租網頁「🏠 房客管理」頁最上方顯示「雲端已連續 3 天正常」後，AI 會交付第二步，之後只剩雲端一封（改版記錄待辦 P3-19）。') if DUP_TRANSITION else ''


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



def firestore_encode(v):
    if v is None: return {'nullValue': None}
    if isinstance(v, bool): return {'booleanValue': v}
    if isinstance(v, int): return {'integerValue': str(v)}
    if isinstance(v, float): return {'doubleValue': v}
    if isinstance(v, str): return {'stringValue': v}
    if isinstance(v, dict): return {'mapValue': {'fields': {k: firestore_encode(x) for k, x in v.items()}}}
    if isinstance(v, (list, tuple)): return {'arrayValue': {'values': [firestore_encode(x) for x in v]}}
    return {'stringValue': str(v)}


def save_fields(fields: dict):
    """把指定欄位寫回 Firestore（只更新這些欄位，其餘不動）"""
    import requests
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ.get('FIREBASE_SERVICE_KEY', '')),
        scopes=['https://www.googleapis.com/auth/datastore'])
    creds.refresh(Request())
    mask = '&'.join(f'updateMask.fieldPaths={k}' for k in fields)
    url = (f'https://firestore.googleapis.com/v1/projects/{PROJECT_ID}'
           f'/databases/(default)/documents/{DOC_PATH}?{mask}')
    body = {'fields': {k: firestore_encode(v) for k, v in fields.items()}}
    r = requests.patch(url, headers={'Authorization': f'Bearer {creds.token}'}, json=body, timeout=30)
    r.raise_for_status()
    print(f'  💾 已寫回 Firestore：{", ".join(fields)}')


def send_mail(subject, body, to=None, kind=KIND_NORMAL):
    if kind != KIND_URGENT and in_quiet_hours():
        raise QuietSkip(f'睡眠時段不寄（{kind}通知，ＡＭ１①）')
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = Header(subject, 'utf-8')
    msg['From'] = GMAIL_ACCOUNT
    msg['To']   = to or NOTIFY_TO
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=30) as s:
        s.login(GMAIL_ACCOUNT, GMAIL_PASSWORD)
        s.sendmail(GMAIL_ACCOUNT, [to or NOTIFY_TO], msg.as_string())
    # ★09230021 待辦 P4-15（鐵律ＡＪ２）：公開倉庫的 Actions 執行紀錄任何人都看得到，主旨含房客姓名與金額 →
    #   預設只印封數；除錯時在 workflow 設環境變數 DEBUG_LOG=1 才印主旨，查完須改回
    print(f'  ✅ 已寄出：{subject}' if os.environ.get('DEBUG_LOG') == '1' else '  ✅ 已寄出 1 封（主旨含個資，公開紀錄不印；設 DEBUG_LOG=1 可看）')


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
            send_mail(dup_head('雲端') + f'📅 契約到期提醒：{RL.get(r, r)} {name}（{left}）', body + dup_foot('雲端')); sent += 1
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
            send_mail(dup_head('雲端') + f'🔧 維護到期提醒：{lbl}（{left}）', body + dup_foot('雲端')); sent += 1
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
        send_mail(dup_head('雲端') + f'💰 每月預存提醒：稅務備用金約 NT${half}', body + dup_foot('雲端')); return 1
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
        send_mail(dup_head('雲端') + f'⏰ 帳單最後應繳日 {today.month}/19 將至，仍有未收帳單', body + dup_foot('雲端')); return 1
    except Exception as e:
        print(f'  ⚠️ 帳單提醒寄送失敗: {e}'); return 0



# ═══════════ ⑤ 哥弟結算通知（動態結算日） ═══════════

def _month_list(a, b):
    """a..b（含）月份字串清單，如 202603..202607"""
    y, m = int(a[:4]), int(a[4:])
    out = []
    while y * 100 + m <= int(b):
        out.append(f'{y}{m:02d}')
        m += 1
        if m > 12: m = 1; y += 1
    return out


def unsettled_net_months(db, today):
    base = str(db.get('settleBaseMonth') or db.get('netBaseMonth') or '202603')  # 結算起算月（房租/水電/網路共用）
    # ★計算日期判定：網路費每月 13 日出帳、25 日扣款。
    #   今天若還沒到出帳日 → 本月尚未發生，不納入（避免多算弟一期）。
    bill_day = int(db.get('netBillDay') or 13)
    _y, _m = today.year, today.month
    if today.day < bill_day:
        _m -= 1
        if _m < 1:
            _m = 12; _y -= 1
    cur = f'{_y}{_m:02d}'
    done = set(str(x) for x in (db.get('netSettledMonths') or []))
    return [m for m in _month_list(base, cur) if m not in done]


def _rent_days(db):
    """在租房客的契約收租日清單"""
    out = []
    for r, t in (db.get('tenants') or {}).items():
        if not t or t.get('status') == 'vacant' or not t.get('name'):
            continue
        d = int(t.get('rentDay') or 0)
        if d:
            out.append(d)
    return out


def settle_days(db, today):
    """回傳 (早鳥結算日, 最終結算日)；自動辨別大小月"""
    y, m = today.year, today.month
    last = calendar.monthrange(y, m)[1]          # 大小月：31/30/29/28
    cost_day = 19 if m % 2 == 0 else 13          # 費用確定日：網路13；偶數月水電19
    # ★09230308 奇數月＋2 天緩衝（中華電信電子帳單不一定準時 13 日寄，主帥 09/21 21:17）；偶數月維持＋1
    early = min(cost_day + (1 if m % 2 == 0 else 2), last)   # 奇數月15 / 偶數月20
    rds = _rent_days(db)
    late = min((max(rds) if rds else 1) + 1, last)   # 最後一位房客收租日+1，壓月底
    return early, max(late, early)


def calc_settle(db, today):
    """只計『未標記已結算』的項目 → 徹底避免重複計算"""
    base  = str(db.get('settleBaseMonth') or db.get('netBaseMonth') or '202603')  # 房租／網路
    bbase = str(db.get('billBaseMonth') or '202602')                              # 水電帳單／公共電費
    # ★兩者不可共用：水電帳單「期別」與網路費「月份」編號基準不同
    rent = 0; lines = []; unpaid = []; periods = []
    pause_keys = []
    for k, p in (db.get('rentPause') or {}).items():
        if p.get('settledTo') and not p.get('bxSettled'):
            amt = int(p.get('settledAmount') or 0)
            rent += amt; pause_keys.append(k); periods.append(str(p.get('settledTo') or ''))
            _tn = (db.get('tenants') or {}).get(p.get('room'), {}).get('name') or ''
            _ms = _month_list(str(p.get('from') or ''), str(p.get('settledTo') or '')) if p.get('from') else []
            lines.append(f"・{RL.get(p.get('room'), p.get('room'))}{('・'+_tn) if _tn else ''} 併收 {len(_ms)} 期（{p.get('from')}～{p.get('settledTo')}）共 {amt:,}")
    rec_idx = []
    for i, rec in enumerate(db.get('rentRecords') or []):
        if rec.get('bxSettled') or str(rec.get('period') or '') < base:
            continue   # ★抓「所有未結算」收租，不再只抓本月
        got = False
        for room, pay in (rec.get('payments') or {}).items():
            amt = int(pay.get('amount') or 0)
            if amt <= 0: continue
            if pay.get('paid'):
                rent += amt; got = True
                periods.append(str(rec.get('period') or ''))
                _tn = (db.get('tenants') or {}).get(room, {}).get('name') or ''
                lines.append(f"・{RL.get(room, room)}{('・'+_tn) if _tn else ''} {rec.get('period')} 房租 {amt:,}")
            else:
                unpaid.append(f'{RL.get(room, room)}（應收 {amt:,}）')
        if got: rec_idx.append(i)
    rent_bro = rent // 2

    adv = odd = pub_bro = pub_big = 0
    bill_idx = []
    unassigned = []; pub_lines = []
    for i, b in enumerate(db.get('utilBills') or []):
        if b.get('bxSettled') or str(b.get('period') or '') < bbase: continue
        if not b.get('utilPaidBy') and int(b.get('n14') or 0) > 0:
            unassigned.append(str(b.get('period') or ''))
        periods.append(str(b.get('period') or ''))
        used = False
        if int(b.get('n9') or 0) or int(b.get('pubBro') or 0) or int(b.get('pubBig') or 0) or int(b.get('n14') or 0):
            used = True
        odd     += int(b.get('n9') or 0)
        _pp = str(b.get('period') or '')
        if int(b.get('pubBro') or 0) > 0:
            pub_lines.append((_pp, f"・{_pp} 期：你代墊 {int(b.get('pubBro')):,}"))
        if int(b.get('pubBig') or 0) > 0:
            pub_lines.append((_pp, f"・{_pp} 期：我代墊 {int(b.get('pubBig')):,}"))
        pub_bro += int(b.get('pubBro') or 0)
        pub_big += int(b.get('pubBig') or 0)
        if b.get('utilPaidBy') == '弟':          # ★只算「明示弟先付」，不再預設弟
            adv += int(b.get('n14') or 0)
        if used: bill_idx.append(i)
    odd_bro = odd // 2 if (db.get('bsOddOwner') == 'half') else 0
    # 哥已還弟的水電代墊：優先取自「轉帳給弟紀錄簿」（水電代墊且尚未納入結算者）
    _bts = [t for t in (db.get('broTransfers') or [])
            if not t.get('settled') and (t.get('kind') or '') == '水電代墊']
    bt_ids = [str(t.get('id')) for t in _bts]
    if _bts:
        paid_back = sum(int(t.get('amount') or 0) for t in _bts)
        _last = max((str(t.get('date') or '') for t in _bts), default='')
        _pp = _last.split('-')
        paid_date = f'{int(_pp[1])}/{int(_pp[2])}' if len(_pp) == 3 else _last
    else:
        paid_back = int(db.get('bsPaidBack') or 0)
        paid_date = (db.get('bsPaidDate') or '').strip()
    util_to_bro = adv - odd_bro - paid_back
    pub_to_bro = pub_bro - ((pub_bro + pub_big) // 2)

    fee = int(db.get('defaultNetFee') or 1409)
    nm = unsettled_net_months(db, today)
    net = fee * len(nm)
    net_bro = net // 2

    common_to_bro = 0; clines = []; com_idx = []
    for i, c in enumerate(db.get('bsCommonCosts') or []):
        if c.get('settled'): continue
        amt = int(c.get('amount') or 0); payer = c.get('payer') or '哥'
        common_to_bro += (amt if payer == '弟' else 0) - amt // 2
        _d = str(c.get('date') or ''); _s = str(c.get('desc') or '')
        _pre = (_d + ' ') if (_d and not _s.startswith(_d)) else ''
        clines.append(f"・{_pre}{_s} ${amt:,}（{'我付' if payer=='哥' else '你付'}，各半 {amt//2:,}）")
        com_idx.append(i)

    transfer = rent_bro + util_to_bro - net_bro + pub_to_bro + common_to_bro
    periods.extend([str(x) for x in nm])
    maxp = max([x for x in periods if x], default='')
    cutoff = ''
    if len(maxp) == 6 and maxp.isdigit():
        _y, _m = int(maxp[:4]), int(maxp[4:])
        cutoff = f'{_y}年{_m}/{calendar.monthrange(_y, _m)[1]}'
    pub_lines.sort(key=lambda x: x[0])
    return dict(cutoff=cutoff, unassigned=unassigned, pub_lines=[t for _, t in pub_lines],
                paid_date=paid_date, bt_ids=bt_ids,
                rent=rent, rent_bro=rent_bro, lines=lines, unpaid=unpaid,
                adv=adv, odd_bro=odd_bro, paid_back=paid_back, util_to_bro=util_to_bro,
                pub_bro=pub_bro, pub_big=pub_big, pub_to_bro=pub_to_bro,
                fee=fee, net_months=nm, net=net, net_bro=net_bro,
                clines=clines, common_to_bro=common_to_bro, transfer=transfer,
                pause_keys=pause_keys, rec_idx=rec_idx, bill_idx=bill_idx, com_idx=com_idx)


def mark_settled(db, c, today):
    """寄信成功後：自動標記已結算 + 存結算單（可在網頁撤銷）"""
    bills = db.get('utilBills') or []
    for i in c['bill_idx']: bills[i]['bxSettled'] = True
    common = db.get('bsCommonCosts') or []
    for i in c['com_idx']: common[i]['settled'] = True
    pause = db.get('rentPause') or {}
    for k in c['pause_keys']: pause[k]['bxSettled'] = True
    recs = db.get('rentRecords') or []
    for i in c['rec_idx']: recs[i]['bxSettled'] = True
    bts = db.get('broTransfers') or []
    _ids = set(c.get('bt_ids') or [])
    for t in bts:
        if str(t.get('id')) in _ids:
            t['settled'] = True
    done = [str(x) for x in (db.get('netSettledMonths') or [])] + c['net_months']
    sett = db.get('settlements') or []
    sett.append({
        'id': f"{today.strftime('%Y%m%d%H%M%S')}",
        'at': today.isoformat(timespec='seconds'),
        'transfer': int(c['transfer']),
        'rent': int(c['rent']), 'adv': int(c['adv']), 'net': int(c['net']),
        'netMonths': c['net_months'],
        'billIdx': c['bill_idx'], 'comIdx': c['com_idx'],
        'pauseKeys': c['pause_keys'], 'recIdx': c['rec_idx'],
        'btIds': c.get('bt_ids') or [],
        'confirmed': False,
    })
    save_fields({'utilBills': bills, 'bsCommonCosts': common, 'rentPause': pause,
                 'rentRecords': recs, 'netSettledMonths': done, 'settlements': sett,
                 'broTransfers': bts})


def check_monthly_settle(db):
    """⑤ 哥弟結算日提醒（★09230308 主帥裁示 P3-8 方案 A：只寄提醒、不自動結算、不寫結算單）
    結算日＝奇數月 15 日／偶數月 20 日（若最後一位房客收租日＋1 更晚，取較晚者）。
    ★舊版在此計算金額並呼叫 mark_settled 自動標記已結算，會寫出網頁無法撤銷的舊格式結算單，
      且與網頁「房東課（哥弟結算）」重複結算 → 停用；calc_settle／mark_settled 保留供待辦 P3-21 驗證用，本函式不再呼叫。"""
    today = datetime.now()
    early, late = settle_days(db, today)
    if today.day != late:
        return 0
    odd = (today.month % 2 == 1)
    what = ('本月為奇數月：只有網路費（中華電信每月 13 日寄電子帳單，已過 2 天緩衝）。' if odd
            else '本月為偶數月：含水、電、瓦斯帳單（台電、台水、瓦斯最後應繳日 19 日已過）。')
    body = (f'今天（{today.month}/{today.day}）是哥弟結算日。\n{what}\n\n'
            '請開啟房租網頁 → 🔑 房東專區 → 房東課（哥弟結算）→ 按「自動套入」，\n'
            '產生給弟的通知信與轉帳金額，確認後儘快轉帳給弟。\n\n'
            '★雲端不再自動結算、不會自動標記已結算（主帥 2026/09/23 裁示），結算一律在網頁完成，避免重複結算。\n\n'
            '—— 嘉義房租雲端通知（奇數月 15 日、偶數月 20 日 20:30 提醒）')
    try:
        send_mail(f'💵 今天是哥弟結算日（{today.year}年{today.month}月）：請到網頁結算並轉帳給弟', body)
        return 1
    except Exception as e:
        print(f'  ⚠️ 結算日提醒寄送失敗: {e}'); return 0

def _hb_url():
    return (f'https://firestore.googleapis.com/v1/projects/{PROJECT_ID}'
            f'/databases/(default)/documents/{HB_PATH}')


def _token():
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ.get('FIREBASE_SERVICE_KEY', '')),
        scopes=['https://www.googleapis.com/auth/datastore'])
    creds.refresh(Request())
    return creds.token


def write_heartbeat(ok, counts, err=''):
    """★雲端心跳：成功與失敗都寫；網頁房客管理頁顯示，超過 26 小時沒成功即紅色警示（ＡＭ６０ 失敗要送到使用者會看的地方）"""
    import requests
    tok = _token(); hdr = {'Authorization': f'Bearer {tok}'}
    r = requests.get(_hb_url(), headers=hdr, timeout=30)
    old = {k: firestore_decode(x) for k, x in r.json().get('fields', {}).items()} if r.status_code == 200 else {}
    now = datetime.now(TW); today = now.strftime('%Y-%m-%d')
    ok_dates = [d for d in (old.get('okDates') or []) if d != today] + ([today] if ok else [])
    ok_dates = ok_dates[-10:]
    streak = 0; d = now.date()
    while d.strftime('%Y-%m-%d') in ok_dates:
        streak += 1; d = d - timedelta(days=1)
    hb = {'version': SCRIPT_VERSION, 'lastRunAt': now.isoformat(timespec='seconds'), 'lastRunOk': bool(ok),
          'lastOkAt': now.isoformat(timespec='seconds') if ok else (old.get('lastOkAt') or ''),
          'lastError': (err or '')[:300], 'sent': counts, 'total': int(sum(counts.values())) if counts else 0,
          'okDates': ok_dates, 'okStreak': streak}
    r = requests.patch(_hb_url(), headers=hdr, json={'fields': {k: firestore_encode(v) for k, v in hb.items()}}, timeout=30)
    r.raise_for_status()
    print(f'  🛰️ 雲端心跳已寫入：ok={ok}、連續 {streak} 天、共寄 {hb["total"]} 封')


def main():
    print(f'▶ 嘉義房租雲端通知 啟動（版本 {SCRIPT_VERSION}）')
    counts = {}; err = ''
    try:
        if not (GMAIL_ACCOUNT and GMAIL_PASSWORD):
            raise RuntimeError('缺少 GMAIL_ACCOUNT / GMAIL_PASSWORD')
        db = load_db()
        print(f'  已讀取（房客 {len(db.get("tenants") or {})}、維護 {len(db.get("maintenanceRecords") or [])}、帳單 {len(db.get("utilBills") or [])}）')
        if in_quiet_hours():
            print('  🌙 睡眠時段（台灣 21:30～07:30）執行：只驗證讀取並寫心跳，不寄任何信（ＡＭ１①⑦；排程班次為 20:30，此情況只會發生在手動執行）')
            counts.update(lease=0, maint=0, reserve=0, bill=0, settle=0)
        else:
            counts['lease'] = check_lease(db)
            counts['maint'] = check_maintenance(db)
            counts['reserve'] = check_reserve(db)
            counts['bill'] = check_bill_deadline(db)
            counts['settle'] = check_monthly_settle(db)
        print(f'✔ 完成：契約 {counts["lease"]}、維護 {counts["maint"]}、預存 {counts["reserve"]}、帳單最後應繳日 {counts["bill"]}、結算日提醒 {counts["settle"]} 封')
    except Exception as e:
        err = f'{type(e).__name__}: {e}'
        raise
    finally:
        try:
            write_heartbeat(not err, counts, err)
        except Exception as he:
            print(f'  ⚠️ 雲端心跳寫入失敗（網頁 26 小時後會顯示紅色警示）: {he}')

if __name__ == '__main__':
    main()
