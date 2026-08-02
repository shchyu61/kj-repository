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
  ⑤ 哥弟結算通知（動態結算日；同時寄給哥與弟）

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


def send_mail(subject, body, to=None):
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = Header(subject, 'utf-8')
    msg['From'] = GMAIL_ACCOUNT
    msg['To']   = to or NOTIFY_TO
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=30) as s:
        s.login(GMAIL_ACCOUNT, GMAIL_PASSWORD)
        s.sendmail(GMAIL_ACCOUNT, [to or NOTIFY_TO], msg.as_string())
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
    base = str(db.get('netBaseMonth') or '202603')     # 網路費起算月（可在網頁調整）
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
    early = min(cost_day + 1, last)              # 奇數月14 / 偶數月20
    rds = _rent_days(db)
    late = min((max(rds) if rds else 1) + 1, last)   # 最後一位房客收租日+1，壓月底
    return early, max(late, early)


def calc_settle(db, today):
    """只計『未標記已結算』的項目 → 徹底避免重複計算"""
    ym = f'{today.year}{today.month:02d}'
    rent = 0; lines = []; unpaid = []
    pause_keys = []
    for k, p in (db.get('rentPause') or {}).items():
        if p.get('settledTo') and not p.get('bxSettled'):
            amt = int(p.get('settledAmount') or 0)
            rent += amt; pause_keys.append(k)
            lines.append(f"　・{RL.get(p.get('room'), p.get('room'))} 併收結清 {amt:,}（收款日 {p.get('settledAt','')}）")
    rec_idx = []
    for i, rec in enumerate(db.get('rentRecords') or []):
        if str(rec.get('period')) != ym or rec.get('bxSettled'):
            continue
        got = False
        for room, pay in (rec.get('payments') or {}).items():
            amt = int(pay.get('amount') or 0)
            if amt <= 0: continue
            if pay.get('paid'):
                rent += amt; got = True
                lines.append(f'　・{RL.get(room, room)} {today.month}月房租 {amt:,}')
            else:
                unpaid.append(f'{RL.get(room, room)}（應收 {amt:,}）')
        if got: rec_idx.append(i)
    rent_bro = rent // 2

    adv = odd = pub_bro = pub_big = 0
    bill_idx = []
    for i, b in enumerate(db.get('utilBills') or []):
        if b.get('bxSettled'): continue
        used = False
        if int(b.get('n9') or 0) or int(b.get('pubBro') or 0) or int(b.get('pubBig') or 0) or int(b.get('n14') or 0):
            used = True
        odd     += int(b.get('n9') or 0)
        pub_bro += int(b.get('pubBro') or 0)
        pub_big += int(b.get('pubBig') or 0)
        if (b.get('utilPaidBy') or '弟') == '弟':
            adv += int(b.get('n14') or 0)
        if used: bill_idx.append(i)
    odd_bro = odd // 2 if (db.get('bsOddOwner') == 'half') else 0
    paid_back = int(db.get('bsPaidBack') or 0)
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
        clines.append(f"　・{c.get('date','')} {c.get('desc','')} {amt:,}（{payer}付，各半 {amt//2:,}）")
        com_idx.append(i)

    transfer = rent_bro + util_to_bro - net_bro + pub_to_bro + common_to_bro
    return dict(rent=rent, rent_bro=rent_bro, lines=lines, unpaid=unpaid,
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
        'confirmed': False,
    })
    save_fields({'utilBills': bills, 'bsCommonCosts': common, 'rentPause': pause,
                 'rentRecords': recs, 'netSettledMonths': done, 'settlements': sett})


def check_monthly_settle(db):
    """⑤ 動態結算日：早鳥日若『哥→弟為正』就結算；否則延到最後一位房客收租日"""
    today = datetime.now()
    early, late = settle_days(db, today)
    if today.day not in (early, late):
        return 0
    c = calc_settle(db, today)
    is_final = (today.day == late)
    if (not is_final) and c['transfer'] < 0:
        print(f'  ⏸ 早鳥日({early})試算為負（費用>收入），延到最終結算日({late})再寄')
        return 0
    if is_final and early != late:
        pass  # 最終日一律寄（早鳥日已寄過的月份，代表當時為正，本日視為補充/最終確認）

    f = lambda n: f'{int(n):,}'
    tag = '最終結算' if is_final else '結算'
    body = (f'【{today.year}年{today.month}月 哥弟{tag}】結算日：{today.month}/{today.day}\n\n'
            f'① 房租（已收訖）合計 {f(c["rent"])}\n' + ('\n'.join(c['lines']) + '\n' if c['lines'] else '')
            + f'　→ 弟半 {f(c["rent_bro"])}（哥 {f(c["rent"] - c["rent_bro"])}）\n\n'
            + f'② 水電瓦斯：弟代墊 {f(c["adv"])}'
            + (f'－零頭弟半 {f(c["odd_bro"])}' if c['odd_bro'] else '')
            + (f'－已還弟 {f(c["paid_back"])}' if c['paid_back'] else '')
            + f' → 補弟 {f(c["util_to_bro"])}\n\n'
            + f'③ 網路費 {f(c["net"])}（哥付，每月13日出帳、25日自動扣款）→ 弟負半 {f(c["net_bro"])}（哥多付1元）\n\n'
            + (f'④ 1樓公共電費：弟墊 {f(c["pub_bro"])}／哥墊 {f(c["pub_big"])} → 淨 {f(c["pub_to_bro"])}\n\n' if (c['pub_bro'] or c['pub_big']) else '')
            + ((f'⑤ 其他共同費用（{len(c["common"])} 筆）\n' + '\n'.join(c['clines'])
                + f'\n　→ 淨 {"哥補弟" if c["common_to_bro"] >= 0 else "弟補哥"} {f(abs(c["common_to_bro"]))}\n\n') if c['common'] else '')
            + f'★ 哥應轉弟：{f(c["transfer"])} 元\n\n')
    if c['unpaid']:
        body += ('⚠️ 本月尚未收訖（不列入本次結算，待收訖後併入次月，非哥方拖延）：\n'
                 + '\n'.join('　・' + u for u in c['unpaid']) + '\n\n')
    body += ('（系統自動試算。已與弟結算完成的併收紀錄，請於網頁「收租歷史」按「✅已與弟結算」標記，'
             '避免下次重複計算。）\n\n—— 嘉義房租雲端通知（動態結算日：'
             f'{"最終結算日 " + str(late) if is_final else "早鳥結算日 " + str(early)} 日）')

    subject = f'💵 {today.year}/{today.month:02d} 哥弟{tag}：哥應轉弟 NT${f(c["transfer"])}'
    sent = 0
    try:
        send_mail(subject, body); sent += 1
    except Exception as e:
        print(f'  ⚠️ 月結算寄送失敗（哥）: {e}')
    bro = (db.get('brotherEmail') or '').strip()
    if bro and db.get('notifyBrother'):
        try:
            send_mail(subject, body, to=bro); sent += 1
        except Exception as e:
            print(f'  ⚠️ 月結算寄送失敗（弟）: {e}')
    if sent:
        try:
            mark_settled(db, c, today)
        except Exception as e:
            print(f'  ⚠️ 自動標記已結算失敗（下次可能重複計算，請至網頁手動標記）: {e}')
    return sent


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
    e = check_monthly_settle(db)
    print(f'✔ 完成：契約 {a}、維護 {b}、預存 {c}、帳單最後應繳日 {d}、哥弟結算 {e} 封')


if __name__ == '__main__':
    main()
