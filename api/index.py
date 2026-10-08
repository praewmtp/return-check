# -*- coding: utf-8 -*-
"""Return-check API (single Vercel Python function).

GET  /api?a=state                -> cards + SR summary (needs login), or {"setup": true} on first run
POST /api?a=setup                -> first-run: set staff PIN and admin PIN
POST /api?a=login                -> PIN + name -> token
POST /api?a=act                  -> staff taps: check / note / pin / platform action
POST /api?a=import               -> admin: parse platform + SR files, preview or commit
POST /api?a=restore              -> admin: load a full backup (cards + staff results)

Uploaded files are parsed in memory and never stored. Only order numbers, tracking numbers,
products, quantities, amounts and dates are kept; buyer names, phones and addresses are dropped.
"""
import os, io, re, json, csv, hmac, hashlib, base64, time, ssl, collections
import datetime as dt
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

STAFF_KEYS = ('check', 'checked_at', 'checked_by', 'note', 'note_at', 'note_by', 'loc', 'loc_at', 'loc_by', 'pinned', 'action', 'action_at', 'action_by', 'sr_confirmed')
TH_MON = ['ม.ค.', 'ก.พ.', 'มี.ค.', 'เม.ย.', 'พ.ค.', 'มิ.ย.', 'ก.ค.', 'ส.ค.', 'ก.ย.', 'ต.ค.', 'พ.ย.', 'ธ.ค.']

# ---------------------------------------------------------------- small helpers

def num(s):
    return float(re.sub(r'[^\d.]', '', str(s if s is not None else '')) or 0)


def s10(v):
    return str(v or '')[:10]


def iso_dmy(s):  # 31/08/2026 12:31:15 -> 2026-08-31
    s = str(s or '')
    m = re.match(r'(\d{2})/(\d{2})/(\d{4})', s)
    return '%s-%s-%s' % (m.group(3), m.group(2), m.group(1)) if m else ''


def iso_lz(s):  # 04 Oct 2026 06:56 -> 2026-10-04
    try:
        return dt.datetime.strptime(str(s), '%d %b %Y %H:%M').date().isoformat()
    except Exception:
        return ''


def read_xlsx(b):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(b), data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], []
    h = [str(x).strip() if x is not None else '' for x in rows[0]]
    return h, [dict(zip(h, r)) for r in rows[1:] if any(v not in (None, '') for v in r)]


# every card has exactly one of these statuses
TYPES = ['ขอคืนสินค้า - รอลูกค้าส่งของกลับ', 'ขอคืนสินค้า - ของกำลังส่งกลับ', 'ขอคืนสินค้า - ของถึงร้านแล้ว', 'คืนเงินอย่างเดียว - ไม่มีของกลับ',
         'ตีกลับ - ของกำลังกลับ', 'ตีกลับ - ของถึงร้านแล้ว', 'ตีกลับ - ยังไม่ทราบว่าถึงร้าน', 'ส่งแล้วยกเลิก - ไม่มีพัสดุตีกลับ']

KIND_LABEL = {
    'sh_rr': 'Shopee คืนเงิน/คืนสินค้า', 'sh_fd': 'Shopee จัดส่งไม่สำเร็จ', 'sh_cc': 'Shopee ยกเลิก',
    'tt_od': 'TikTok คำสั่งซื้อที่ยกเลิก', 'tt_rt': 'TikTok คืนเงิน/คืนสินค้า',
    'lz_od': 'Lazada คำสั่งซื้อ', 'lz_rt': 'Lazada คืนสินค้า', 'sr': 'SR (Express)',
}
ORDER = ['sh_fd', 'sh_cc', 'sh_rr', 'tt_od', 'tt_rt', 'lz_od', 'lz_rt', 'sr']


def detect(b):
    """Decide what a file is from its column headers, never from its name."""
    if b[:2] == b'PK':
        h, rows = read_xlsx(b)
        hs = set(h)
        if 'หมายเลขคำขอคืนเงิน/คืนสินค้า' in hs: return 'sh_rr', rows
        if 'จัดส่งไม่สำเร็จ' in hs and 'หมายเลขคำสั่งซื้อ' in hs: return 'sh_fd', rows
        if 'เหตุผลในการยกเลิกคำสั่งซื้อ' in hs and 'หมายเลขคำสั่งซื้อ' in hs: return 'sh_cc', rows
        if 'Return Order ID' in hs and 'Return Logistics Tracking ID' in hs: return 'tt_rt', rows
        if 'Order ID' in hs and 'Cancelation/Return Type' in hs: return 'tt_od', rows
        if 'orderItemId' in hs and 'orderNumber' in hs: return 'lz_od', rows
        if 'Return Order ID' in hs and 'Return Item ID' in hs: return 'lz_rt', rows
        return None, []
    for enc in ('cp874', 'utf-8-sig'):
        try:
            t = b.decode(enc)
        except Exception:
            continue
        if 'รายงานใบลดหนี้' in t:
            return 'sr', t
    return None, []


# ---------------------------------------------------------------- platform files -> cards

def _card(**k):
    base = dict(out_track='', req='-', ret_track='-', req_date='', refund_date='', cancel_date='', ship_date='', dispute_by='', reason='', ret_arrived='')
    base.update(k)
    return base


def build_sh_rr(rows, st):
    seen = collections.OrderedDict()
    for r in rows:
        seen[(r['หมายเลขคำขอคืนเงิน/คืนสินค้า'], r.get('ชื่อสินค้า'), r.get('ชื่อตัวเลือกสินค้า'))] = r
    cards = collections.OrderedDict()
    for r in seen.values():
        st['rows'] += 1
        s = r.get('สถานะการคืนเงินหรือคืนสินค้า')
        offer = r.get('ข้อเสนอการคืนเงิน/คืนสินค้า')
        lg = r.get('สถานะการส่งสินค้าคืน')
        if s == 'ยกเลิกคำขอ':
            st['skip']['ลูกค้ายกเลิกคำขอ'] += 1
            continue
        if s != 'คืนเงินแล้ว':
            rank, urg, typ = 1, 'ด่วนมาก', 'ขอคืนสินค้า - ของกำลังส่งกลับ'
            todo = 'เฝ้ารับพัสดุ เปิดตรวจทันทีที่ถึง ถ้าของไม่ครบ/ไม่ใช่ของร้าน แจ้งแอดมินวันนั้นเลย'
        elif lg == 'จัดส่งสินค้าคืนสำเร็จ':
            rank, urg, typ = 2, 'ด่วน', 'ขอคืนสินค้า - ของถึงร้านแล้ว'
            todo = 'ค้นหาพัสดุขาคืน ตรวจว่าได้ของจริง ครบ และสภาพดี'
        else:
            rank, urg, typ = 4, 'เพื่อทราบ', 'คืนเงินอย่างเดียว - ไม่มีของกลับ'
            todo = 'ไม่มีของกลับ คลังไม่ต้องหา (แอดมินตรวจเหตุผล/ยื่นข้อพิพาท)'
        req = str(r['หมายเลขคำขอคืนเงิน/คืนสินค้า'])
        cid = 'rr-' + req
        if cid not in cards:
            d = r.get('เวลาที่จัดส่งสินค้าคืนสำเร็จ') or r.get('เวลายื่นคำขอคืนเงิน/คืนสินค้า')
            cards[cid] = _card(rank=rank, urg=urg, typ=typ, platform='Shopee', order=str(r['หมายเลขคำสั่งซื้อ']),
                               out_track=str(r.get('หมายเลขติดตามพัสดุจากผู้ขาย') or ''), req=req,
                               ret_track=str(r.get('หมายเลขติดตามพัสดุสำหรับส่งคืน') or '-'),
                               status='%s / %s' % (s, lg or 'ไม่มีการส่งคืน'), date=s10(d),
                               req_date=s10(r.get('เวลายื่นคำขอคืนเงิน/คืนสินค้า')), refund_date=s10(r.get('เวลาที่คืนเงิน')),
                               refund_amt=num(r.get('จำนวนเงินคืนทั้งหมด')), dispute_by=s10(r.get('ผู้ขายสามารถยื่นข้อพิพาทได้ภายใน')),
                               ret_arrived=s10(r.get('เวลาที่จัดส่งสินค้าคืนสำเร็จ')),
                               reason=str(r.get('เหตุผลในการขอคืนสินค้า') or ''), todo=todo, items=[])
        cards[cid]['items'].append(dict(name=str(r.get('ชื่อสินค้า') or ''), var=str(r.get('ชื่อตัวเลือกสินค้า') or '-'), qty=int(num(r.get('จำนวนสินค้าคืน')))))
        st['card_rows'] += 1
    return cards


def build_sh_fd(rows, st):
    seen = collections.OrderedDict()
    for r in rows:
        seen[(r['หมายเลขคำสั่งซื้อ'], r.get('ชื่อสินค้า'), r.get('ชื่อตัวเลือก'))] = r
    cards = collections.OrderedDict()
    for r in seen.values():
        st['rows'] += 1
        s = r.get('จัดส่งไม่สำเร็จ') or ''
        moving = s != 'จัดส่งคืนผู้ขายแล้ว'
        cid = 'fd-' + str(r['หมายเลขคำสั่งซื้อ'])
        if cid not in cards:
            cards[cid] = _card(rank=3, urg='ตรวจ', typ='ตีกลับ - ' + ('ของกำลังกลับ' if moving else 'ของถึงร้านแล้ว'),
                               platform='Shopee', order=str(r['หมายเลขคำสั่งซื้อ']), out_track=str(r.get('*หมายเลขติดตามพัสดุ') or ''),
                               ret_track='(ใช้เลขเดิม)', status=s, date=s10(r.get('เวลาส่งสินค้า')), ship_date=s10(r.get('เวลาส่งสินค้า')),
                               reason='จัดส่งไม่สำเร็จ', plat_amt=0.0, items=[],
                               todo='รอรับพัสดุตีกลับ' if moving else 'ค้นหาพัสดุตีกลับ ตรวจว่าได้ของจริง ครบ แล้วเก็บเข้าสต็อก')
        cards[cid]['items'].append(dict(name=str(r.get('ชื่อสินค้า') or ''), var=str(r.get('ชื่อตัวเลือก') or '-'), qty=int(num(r.get('จำนวน')))))
        cards[cid]['plat_amt'] = round(cards[cid]['plat_amt'] + num(r.get('ราคาขายสุทธิ')), 2)
        st['card_rows'] += 1
    return cards


def build_sh_cc(rows, st, failed_orders):
    seen = collections.OrderedDict()
    for r in rows:
        seen[(r['หมายเลขคำสั่งซื้อ'], r.get('ชื่อสินค้า'), r.get('ชื่อตัวเลือก'))] = r
    cards = collections.OrderedDict()
    for r in seen.values():
        st['rows'] += 1
        o = str(r['หมายเลขคำสั่งซื้อ'])
        if r.get('เวลาส่งสินค้า') in (None, '', '-'):
            st['skip']['ยกเลิกก่อนส่งของ'] += 1
            continue
        if o in failed_orders:
            st['skip']['อยู่ในไฟล์จัดส่งไม่สำเร็จแล้ว (การ์ดเดียวกัน)'] += 1
            continue
        cid = 'fd-' + o
        if cid not in cards:
            cards[cid] = _card(rank=4, urg='เพื่อทราบ', typ='ส่งแล้วยกเลิก - ไม่มีพัสดุตีกลับ', platform='Shopee', order=o,
                               out_track=str(r.get('*หมายเลขติดตามพัสดุ') or ''), status='ยกเลิกแล้ว', date=s10(r.get('เวลาส่งสินค้า')),
                               ship_date=s10(r.get('เวลาส่งสินค้า')), reason=str(r.get('เหตุผลในการยกเลิกคำสั่งซื้อ') or '').replace('<br>', ' '),
                               plat_amt=0.0, items=[], todo='แอดมินตรวจ: น่าจะพัสดุสูญหาย/ได้ค่าชดเชย ของอาจไม่กลับ')
        cards[cid]['items'].append(dict(name=str(r.get('ชื่อสินค้า') or ''), var=str(r.get('ชื่อตัวเลือก') or '-'), qty=int(num(r.get('จำนวน')))))
        cards[cid]['plat_amt'] = round(cards[cid]['plat_amt'] + num(r.get('ราคาขายสุทธิ')), 2)
        st['card_rows'] += 1
    return cards


def build_tt_od(rows, st):
    seen = collections.OrderedDict()
    for r in rows:
        if not str(r.get('Order ID') or '').isdigit():
            continue  # row 2 of the export is a description row
        seen[(r['Order ID'], r.get('SKU ID'), r.get('Variation'))] = r
    cards = collections.OrderedDict()
    for r in seen.values():
        st['rows'] += 1
        if not r.get('Shipped Time'):
            st['skip']['ยกเลิกก่อนส่งของ'] += 1
            continue
        o = str(r['Order ID'])
        cid = 'tt-' + o
        reason = str(r.get('Cancel Reason') or '')
        if cid not in cards:
            cards[cid] = _card(rank=3, urg='ตรวจ',
                               typ='ตีกลับ - ยังไม่ทราบว่าถึงร้าน',
                               platform='TikTok', order=o, out_track=str(r.get('Tracking ID') or ''), ret_track='(ใช้เลขเดิม)',
                               status='ยกเลิกแล้ว: ' + reason, date=iso_dmy(r.get('Cancelled Time')), cancel_date=iso_dmy(r.get('Cancelled Time')),
                               ship_date=iso_dmy(r.get('Shipped Time')),
                               reason='%s: %s · %s · %s' % (r.get('Cancel By') or '', reason, r.get('Shipping Provider Name') or '', r.get('Payment Method') or ''),
                               todo='ค้นหาพัสดุตีกลับ ตรวจว่าได้ของจริง ครบ แล้วเก็บเข้าสต็อก', plat_amt=0.0, items=[])
        cards[cid]['items'].append(dict(name=str(r.get('Product Name') or ''), var=str(r.get('Variation') or '-'), qty=int(num(r.get('Quantity')))))
        cards[cid]['plat_amt'] = round(cards[cid]['plat_amt'] + num(r.get('SKU Subtotal After Discount')), 2)
        st['card_rows'] += 1
    return cards


TT_REASON = {"Product is defective or doesn't work": 'สินค้าชำรุด/ใช้ไม่ได้', 'Change of mind': 'เปลี่ยนใจ', 'Wrong product sent': 'ส่งสินค้าผิด',
             "Product doesn't match description": 'สินค้าไม่ตรงคำอธิบาย', 'Received parcel, but some items were missing': 'ได้รับพัสดุ แต่สินค้าบางชิ้นขาด'}


def build_tt_rt(rows, st):
    cards = collections.OrderedDict()
    for r in rows:
        st['rows'] += 1
        s = r.get('Return Status') or ''
        sub = r.get('Return Sub Status') or ''
        rt = r.get('Return Type') or ''
        trk = str(r.get('Return Logistics Tracking ID') or '')
        if not s:
            st['skip']['คำขอว่าง ไม่มีสถานะ'] += 1
            continue
        if s == 'Refund rejected':
            st['skip']['คำขอถูกปฏิเสธ/ลูกค้ายกเลิก'] += 1
            continue
        if rt == 'Refund only':
            rank, urg, typ, todo = 4, 'เพื่อทราบ', 'คืนเงินอย่างเดียว - ไม่มีของกลับ', 'ไม่มีของกลับ คลังไม่ต้องหา (แอดมินตรวจเหตุผล/ยื่นข้อพิพาท)'
        elif s == 'Completed' and not trk:
            rank, urg, typ, todo = 4, 'เพื่อทราบ', 'คืนเงินอย่างเดียว - ไม่มีของกลับ', 'TikTok คืนเงินโดยไม่มีพัสดุส่งคืน คลังไม่ต้องหา (แอดมินตรวจ)'
        elif s == 'Completed':
            rank, urg, typ, todo = 2, 'ด่วน', 'ขอคืนสินค้า - ของถึงร้านแล้ว', 'ค้นหาพัสดุขาคืน ตรวจว่าได้ของจริง ครบ และสภาพดี'
        elif trk:
            rank, urg, typ, todo = 1, 'ด่วนมาก', 'ขอคืนสินค้า - ของกำลังส่งกลับ', 'เฝ้ารับพัสดุ เปิดตรวจทันทีที่ถึง ถ้าของไม่ครบ/ไม่ใช่ของร้าน แจ้งแอดมินวันนั้นเลย'
        else:
            rank, urg, typ, todo = 1, 'ด่วนมาก', 'ขอคืนสินค้า - รอลูกค้าส่งของกลับ', 'ยังไม่มีเลขพัสดุ รอลูกค้าส่งของ เมื่อของถึงให้เปิดตรวจทันที'
        comp = str(r.get('Compensation Status') or '')
        reason = TT_REASON.get(r.get('Return Reason'), str(r.get('Return Reason') or ''))
        if comp.startswith('Successful'):
            reason += ' · TikTok ชดเชยร้าน %s' % (r.get('Compensation Amount') or '')
        if r.get('Dispute Status'):
            reason += ' · ข้อพิพาท: %s' % r.get('Dispute Status')
        if r.get('Appeal Status'):
            reason += ' · อุทธรณ์: %s' % r.get('Appeal Status')
        req = str(r['Return Order ID'])
        cards['tr-' + req] = _card(rank=rank, urg=urg, typ=typ, platform='TikTok', order=str(r['Order ID']), req=req, ret_track=trk or '-',
                                   status='%s / %s' % (rt, s) + (' / ' + sub if sub else ''), date=iso_dmy(r.get('Time Requested')),
                                   req_date=iso_dmy(r.get('Time Requested')), refund_date=iso_dmy(r.get('Refund Time')),
                                   refund_amt=num(r.get('Return unit price')), reason=reason, todo=todo,
                                   items=[dict(name=str(r.get('Product Name') or ''), var=str(r.get('SKU Name') or '-'), qty=int(num(r.get('Return Quantity'))))])
        st['card_rows'] += 1
    return cards


def build_lz_rt(rows, st):
    by = collections.OrderedDict()
    for r in rows:
        st['rows'] += 1
        by.setdefault(str(r['Return Order ID']), []).append(r)
    cards = collections.OrderedDict()
    for req, rs in by.items():
        r = rs[0]
        if all((x.get('Status') or '') == 'ReturnClosed' for x in rs):
            st['skip']['คำขอถูกปิด (ReturnClosed)'] += len(rs)
            continue
        lg = r.get('Logistic Status') or ''
        trk = str(r.get('Tracking Number') or '')
        refunded = any((x.get('Status') or '') == 'Refunded' for x in rs)
        if lg:
            rank, urg, typ, todo = 2, 'ด่วน', 'ขอคืนสินค้า - ของถึงร้านแล้ว', 'ค้นหาพัสดุขาคืน ตรวจว่าได้ของจริง ครบ และสภาพดี'
        else:
            rank, urg, typ, todo = 4, 'เพื่อทราบ', 'คืนเงินอย่างเดียว - ไม่มีของกลับ', 'ไม่มีของกลับ คลังไม่ต้องหา (แอดมินตรวจเหตุผล/ยื่นข้อพิพาท)'
        items = collections.OrderedDict()
        for x in rs:
            sku = str(x.get('Seller SKU ID') or '')
            var = ('รหัส SKU ' + sku) if re.match(r'^[\d\-_]+$', sku) else (sku or '-')
            k = (str(x.get('Item Name') or ''), var)
            items[k] = items.get(k, 0) + 1
        cards['lr-' + req] = _card(rank=rank, urg=urg, typ=typ, platform='Lazada', order=str(r['Order ID']), req=req, ret_track=trk or '-',
                                   status='%s / %s' % (r.get('Status') or '', lg or 'ไม่มีการส่งคืน'), date=s10(r.get('Return Order Date')),
                                   req_date=s10(r.get('Return Order Date')), refunded=refunded,
                                   refund_amt=round(sum(num(x.get('Refund Amount')) for x in rs), 2), reason=str(r.get('Return Reason') or ''),
                                   todo=todo, items=[dict(name=k[0], var=k[1], qty=v) for k, v in items.items()])
        st['card_rows'] += len(rs)
    return cards


def build_lz_od(rows, st):
    by = collections.OrderedDict()
    for r in rows:
        st['rows'] += 1
        trk = r.get('trackingCode') or r.get('cdTrackingCode') or r.get('trackingCodeFM')
        status = str(r.get('status') or '')
        if not trk:
            st['skip']['ยกเลิกก่อนส่งของ'] += 1
            continue
        if status.lower() in ('delivered', 'shipped', 'ready_to_ship', 'pending'):
            st['skip']['สถานะ %s ไม่ใช่ตีกลับ' % status] += 1
            continue
        by.setdefault(str(r['orderNumber']), []).append(r)
    cards = collections.OrderedDict()
    for o, rs in by.items():
        r = rs[0]
        status = str(r.get('status') or '')
        back = status == 'Package Returned'
        items = collections.OrderedDict()
        for x in rs:
            k = (str(x.get('itemName') or x.get('sellerSku') or ''), str(x.get('variation') or x.get('sellerSku') or '-'))
            items[k] = items.get(k, 0) + 1
        d = iso_lz(r.get('updateTime'))
        cards['lf-' + o] = _card(rank=3, urg='ตรวจ', typ='ตีกลับ - ' + ('ของถึงร้านแล้ว' if back else 'ยังไม่ทราบว่าถึงร้าน'), platform='Lazada',
                                 order=o, out_track=str(r.get('trackingCode') or r.get('cdTrackingCode') or r.get('trackingCodeFM') or ''),
                                 ret_track='(ใช้เลขเดิม)', status=status, date=d, cancel_date=d, ship_date=iso_lz(r.get('createTime')),
                                 reason=str(r.get('buyerFailedDeliveryReason') or ''), todo='ค้นหาพัสดุตีกลับ ตรวจว่าได้ของจริง ครบ แล้วเก็บเข้าสต็อก',
                                 plat_amt=round(sum(num(x.get('paidPrice')) for x in rs), 2), items=[dict(name=k[0], var=k[1], qty=v) for k, v in items.items()])
        st['card_rows'] += len(rs)
    return cards


# ---------------------------------------------------------------- SR (Express) report

ORD_RE = re.compile(r'^(\d{6}[0-9A-Z]{8}|5\d{17}|\d{15,16})(?:-\d)?(?:\s*/\s*(.+))?$')


def parse_sr(text):
    """Express 'รายงานใบลดหนี้/รับคืนสินค้า' CSV -> list of SR docs. Order numbers sit UNDER the item lines
    and apply to every item line since the previous run of order numbers."""
    rows = list(csv.reader(io.StringIO(text, newline='')))
    srs, cust, cur, pend, lastref, rmk = [], None, None, [], False, False
    for r in rows:
        r = [c.replace('\xa0', ' ').strip() for c in r]
        if not any(r):
            continue
        if len(r) > 1 and (r[1].startswith('STT') or 'รายงานใบลดหนี้' in r[1] or r[1].startswith('วันที่จาก') or r[1].startswith('รหัสลูกค้า')
                           or r[1].startswith('พนักงานขาย') or r[1] == 'ชื่อลูกค้า'):
            continue
        if len(r) > 5 and r[4] == 'คืน' and r[5] == 'ลำดับ':
            continue
        if len(r) == 3 and r[1] and re.match(r'^[\d.]+$', r[2] or ''):
            cust = (r[1], r[2])
            continue
        if len(r) > 5 and re.match(r'^\*?SR\d{9}$', r[5] or ''):
            cur = dict(sr=r[5].lstrip('*'), void=r[5].startswith('*'), date=r[6], cust=cust[0] if cust else '', cust_code=cust[1] if cust else '',
                       total=num(r[13]) if len(r) > 13 else 0.0, lines=[], remark_orders=[])
            srs.append(cur)
            pend, lastref, rmk = [], False, False
            continue
        if len(r) > 8 and r[4] in ('Y', 'N') and r[5].isdigit() and cur:
            if lastref:
                pend = []
            rmk = False
            neg = -1 if str(r[12] if len(r) > 12 else '').startswith('-') else 1
            ln = dict(ret=r[4], no=int(r[5]), code=r[6], name=r[7], qty=num(r[8]), unit=r[9], price=num(r[10]),
                      amt=neg * num(r[12]) if len(r) > 12 else 0.0, orders=[])
            cur['lines'].append(ln)
            pend.append(ln)
            lastref = False
            continue
        if len(r) > 5 and r[5].startswith('หมายเหตุ') and cur:
            rmk = True
            continue
        if len(r) > 6 and r[6] and not r[4] and not r[5] and cur:
            m = ORD_RE.match(r[6])
            if m and rmk:
                cur['remark_orders'].append(m.group(1))
                continue
            if m:
                grp = 'GROUP' if len(pend) > 1 else 'EXACT'
                for ln in (pend if pend else cur['lines'][-1:]):
                    ln['orders'].append(dict(order=m.group(1), hint=m.group(2) or '', link=grp))
                lastref = True
            continue
    return srs


def sr_online(s):
    return s.get('cust', '').startswith('ออนไลน์')


def sr_iso(d):  # 22/08/2569 -> 2026-08-22
    m = re.match(r'(\d{2})/(\d{2})/(\d{4})', d or '')
    return '%04d-%s-%s' % (int(m.group(3)) - 543, m.group(2), m.group(1)) if m else ''


COLORS = ['ขาว', 'ดำ', 'น้ำตาล', 'เขียว', 'ฟ้า', 'น้ำเงิน', 'เทา', 'ชมพู', 'เหลือง', 'แดง', 'ส้ม', 'ม่วง', 'กรม', 'ครีม']
NOT_CODE = {'4000', '3000', '1000', '2569', '1200'}
RANK = {'ok': 0, 'eye': 1, 'diff': 2}


def _codes(t):
    return {x for x in re.findall(r'(?<!\d)(\d{4})(?!\d)', t.replace(',', '')) if x not in NOT_CODE}


def _colors(t):
    t = t.replace('นำ้', 'น้ำ')
    return {c for c in COLORS if c in t}


def _fam(t):
    t = t.replace(',', '').lower()
    if re.search(r'4000\s*(m|เมตร)|4000m', t):
        return '4000m'
    for g in ('120', '250', '500', '650', '200', '167'):
        if re.search(g + r'\s*(g|กรัม)', t):
            return g + 'g'
    return ''


def _sizeno(t):
    t = t.replace('½', ' 1/2')
    m = re.search(r'(?:เบอร์|#)\s*(\d+(?:\s*1/2)?)', t)
    return re.sub(r'\s+', ' ', m.group(1)).strip() if m else ''


def _pack(i):
    m = re.search(r'[\[\(]\s*(\d+)\s*หลอด', i['var']) or re.search(r'[\[\(]\s*(\d+)\s*หลอด', i['name'])
    return int(m.group(1)) if m else 1


def _base_plat(i):
    return i['qty'] * _pack(i)


def _base_sr(l):
    return l['qty'] * 12 if l['unit'] == 'โหล' else (None if l['unit'] == 'ลัง' else l['qty'])


def prod_cmp(i, l):
    """Platform is the source of truth. Returns (ok|diff|eye, reason)."""
    p = i['name'] + ' ' + i['var']
    s = l['code'] + ' ' + l['name']
    pc, sc = _codes(p), _codes(s)
    sn_p, sn_s = _sizeno(i['var']) or _sizeno(i['name']), _sizeno(l['name'])
    fp, fs = _fam(i['var']) or _fam(i['name']), _fam(l['name'])
    if sn_p and sn_s:
        if sn_p != sn_s:
            return 'diff', 'เบอร์ต่าง: แพลตฟอร์ม %s / SR %s' % (sn_p, sn_s)
        return 'ok', 'เบอร์ %s ตรง' % sn_p
    if pc and sc:
        if pc & sc:
            if fp and fs and fp != fs:
                return 'diff', 'รหัสสี %s ตรง แต่ขนาดต่าง: แพลตฟอร์ม %s / SR %s' % ('/'.join(sorted(pc & sc)), fp, fs)
            return 'ok', 'รหัสสี %s ตรง' % '/'.join(sorted(pc & sc))
        if '1307' in pc and '1306' in sc and 'ดำ' in s:  # known SR habit, confirmed: platform A1307 is right
            if fp and fs and fp != fs:
                return 'diff', 'สีดำตรง (A1307) แต่ขนาดต่าง: แพลตฟอร์ม %s / SR %s' % (fp, fs)
            return 'ok', 'สีดำ A1307 ตรง (SR เขียนเป็น A1306 ซึ่งผิด ยึดแพลตฟอร์ม: A1307)'
        return 'diff', 'รหัสสีต่าง: แพลตฟอร์ม %s / SR %s' % ('/'.join(sorted(pc)), '/'.join(sorted(sc)))
    cp, cs = _colors(i['var']), _colors(l['name'])
    if cp and cs:
        if cp & cs:
            if fp and fs and fp != fs:
                return 'diff', 'สี%sตรง แต่ขนาดต่าง: แพลตฟอร์ม %s / SR %s' % ('/'.join(sorted(cp & cs)), fp, fs)
            return 'ok', 'สี%sตรง' % '/'.join(sorted(cp & cs))
        return 'diff', 'สีต่าง: แพลตฟอร์ม %s / SR %s' % ('/'.join(sorted(cp)), '/'.join(sorted(cs)))
    for k in ('มอเตอร์', 'ตีนผี', 'เข็ม', 'ซิป', 'กรรไกร', 'มัด', 'ฟูน้อย', 'ด้ายมัน', 'เชือก', 'กระโหลก', 'กระสวย'):
        if k in p and k in s:
            return 'ok', 'ชนิดสินค้าตรง (%s)' % k
    return 'eye', 'ไม่มีรหัสให้เทียบอัตโนมัติ ให้ดูด้วยตา'


def card_amt(cid, c):
    return c.get('refund_amt') if cid[:2] in ('rr', 'tr', 'lr') else c.get('plat_amt')


def verify_sr(cards, staff, srs):
    """cards: id -> data, staff: id -> staff dict, srs: list of SR docs.
    Returns (id -> sr dict, summary dict). Matching is by the order number written under each SR line."""
    online = [s for s in srs if sr_online(s)]
    ref = collections.defaultdict(list)
    for s in online:
        for l in s['lines']:
            for o in l['orders']:
                ref[o['order']].append((s, l, o))
        for o in s.get('remark_orders', []):
            ref[o].append((s, None, {'order': o, 'hint': '', 'link': 'REMARK'}))
    by_order = collections.defaultdict(list)
    for cid, c in cards.items():
        by_order[c['order']].append((cid, c))
    out = {}
    for cid, c in cards.items():
        rs = ref.get(c['order'], [])
        if not rs:
            continue
        lines = [(s, l, o) for s, l, o in rs if l]
        pa = card_amt(cid, c)
        res = dict(prod='eye', qty='eye', price='eye')
        notes, used, pv, qv = [], set(), [], []
        for i in c['items']:
            best = None
            for k, (s, l, o) in enumerate(lines):
                v, w = prod_cmp(i, l)
                if best is None or RANK[v] < RANK[best[0]] or (RANK[v] == RANK[best[0]] and k not in used and best[2] in used):
                    best = (v, w, k)
            if best is None:
                pv.append('eye')
                notes.append('สินค้า: SR อ้างคำสั่งซื้อนี้ในหมายเหตุ ไม่ผูกกับบรรทัดสินค้า')
                continue
            v, w, k = best
            used.add(k)
            s, l, o = lines[k]
            pv.append(v)
            notes.append('สินค้า [%s]: %s' % (i['var'], w))
            bp, bs, n = _base_plat(i), _base_sr(l), len(l['orders'])
            tube = l['unit'] in ('โหล', 'หลอด')
            if bs is None:
                qv.append('eye')
                notes.append('จำนวน: SR หน่วยลัง เทียบอัตโนมัติไม่ได้')
            elif n == 1:
                if abs(bp - bs) < 0.01:
                    qv.append('ok')
                    notes.append('จำนวน [%s]: ตรง %g%s' % (i['var'], bp, ' หลอด' if tube else ' ' + l['unit']))
                else:
                    qv.append('diff')
                    notes.append('จำนวน [%s]: ไม่ตรง แพลตฟอร์ม %g / SR %g %s%s' % (i['var'], bp, l['qty'], l['unit'], ' (=%g หลอด)' % bs if l['unit'] == 'โหล' else ''))
            else:
                others = [x['order'] for x in l['orders'] if x['order'] != c['order']]
                if all(x in by_order for x in others):
                    tot = bp
                    for x in others:
                        for _, cc in by_order[x]:
                            for ii in cc['items']:
                                if RANK[prod_cmp(ii, l)[0]] < 2:
                                    tot += _base_plat(ii)
                    if abs(tot - bs) < 0.01:
                        qv.append('ok')
                        notes.append('จำนวน: บรรทัด SR ใช้ร่วม %d คำสั่งซื้อ รวมกันตรง %g' % (n, bs))
                    else:
                        qv.append('diff')
                        notes.append('จำนวน: บรรทัด SR ใช้ร่วม %d คำสั่งซื้อ รวมแพลตฟอร์ม %g / SR %g' % (n, tot, bs))
                else:
                    qv.append('eye')
                    notes.append('จำนวน: บรรทัด SR ใช้ร่วม %d คำสั่งซื้อ (บางใบไม่อยู่ในหน้านี้) เทียบอัตโนมัติไม่ได้' % n)
        if pv:
            res['prod'] = max(pv, key=lambda v: RANK[v])
        if qv:
            res['qty'] = max(qv, key=lambda v: RANK[v])
        ul = [lines[k] for k in sorted(used)]
        extra = [lines[k] for k in range(len(lines)) if k not in used]
        if extra:
            notes.append('SR มีบรรทัดอื่นที่อ้างคำสั่งซื้อนี้อีก %d บรรทัด: ' % len(extra) + ', '.join('%s %g %s' % (l['code'], l['qty'], l['unit']) for s, l, o in extra))
            res['prod'] = max(res['prod'], 'eye', key=lambda v: RANK[v])
        if ul and all(len(l['orders']) == 1 for s, l, o in ul) and not extra and pa is not None:
            sa = round(sum(l['amt'] for s, l, o in ul), 2)
            if abs(sa - pa) <= 1:
                res['price'] = 'ok'
                notes.append('ราคา: ตรง ฿%.2f' % sa)
            else:
                res['price'] = 'diff'
                notes.append('ราคา: SR ฿%.2f / แพลตฟอร์ม ฿%.2f (ต่าง ฿%.2f)' % (sa, pa, sa - pa))
        elif ul:
            notes.append('ราคา: บรรทัด SR ใช้ร่วมหลายคำสั่งซื้อ เทียบอัตโนมัติไม่ได้ (แพลตฟอร์ม ฿%.2f)' % (pa or 0))
        # an admin can confirm "SR wrote the product wrong, goods match the platform"
        conf = (staff.get(cid) or {}).get('sr_confirmed') or ''
        if 'prod' in conf and res['prod'] != 'ok':
            res['prod'] = 'ok'
            notes.insert(0, 'สินค้า: ยืนยันแล้วว่า SR เขียนผิด ยึดข้อมูลแพลตฟอร์ม')
        srl = [dict(sr=s['sr'], date=s['date'], no=l['no'], code=l['code'], name=l['name'], qty=l['qty'], unit=l['unit'], price=l['price'],
                    amt=l['amt'], ret=l['ret'], shared=len(l['orders'])) for s, l, o in lines]
        srnos = sorted({s['sr'] for s, l, o in rs})
        sd = {s['sr']: s['date'] for s, l, o in rs}
        worst = max(res.values(), key=lambda v: RANK[v])
        out[cid] = dict(sr=', '.join(srnos), sr_date=', '.join(sd[x] for x in srnos), sr_lines=srl, sr_chk=res, sr_notes=notes, sr_state=worst)
    plat = lambda o: 'TikTok' if (o.isdigit() and len(o) == 18) else ('Lazada' if o.isdigit() else 'Shopee')
    orph = []
    for s in online:
        for l in s['lines']:
            for o in l['orders']:
                if o['order'] not in by_order:
                    orph.append(dict(order=o['order'], platform=plat(o['order']), sr=s['sr'], date=s['date'], no=l['no'], code=l['code'], name=l['name'],
                                     qty=l['qty'], unit=l['unit'], amt=l['amt'], shared=len(l['orders']), hint=o['hint']))
        for o in s.get('remark_orders', []):
            if o not in by_order:
                orph.append(dict(order=o, platform=plat(o), sr=s['sr'], date=s['date'], no=0, code='(หมายเหตุ)', name='อ้างในหมายเหตุของ SR', qty=0, unit='', amt=0, shared=0, hint=''))
    noref = [dict(sr=s['sr'], date=s['date'], no=l['no'], code=l['code'], name=l['name'], qty=l['qty'], unit=l['unit'], amt=l['amt'])
             for s in online for l in s['lines'] if not l['orders']]
    last = max([sr_iso(s['date']) for s in srs] or [''])
    to = '%d %s %d' % (int(last[8:10]), TH_MON[int(last[5:7]) - 1], int(last[:4]) + 543) if last else ''
    bad_total = [s['sr'] for s in srs if s['lines'] and abs(sum(l['amt'] for l in s['lines']) - s['total']) > 0.01]
    meta = dict(to=to, sr_total=len(srs), sr_online=len(online), orph=orph, noref=noref, bad_total=bad_total)
    return out, meta


# ---------------------------------------------------------------- import pipeline (pure: no database here)

def run_import(files, cards, staff, sr_docs):
    """files: [(name, bytes)]. cards/staff: id -> dict (current database). sr_docs: {sr_no: doc}.
    Returns (new_cards, new_sr_docs, sr_by_card, sr_meta, report). Nothing is written here."""
    parsed, unknown = [], []
    for name, b in files:
        try:
            kind, rows = detect(b)
        except Exception as e:
            kind, rows = None, []
        if not kind:
            unknown.append(name)
        else:
            parsed.append((ORDER.index(kind), name, kind, rows, hashlib.md5(b).hexdigest()))
    parsed.sort(key=lambda x: x[0])
    new_cards = collections.OrderedDict((k, dict(v)) for k, v in cards.items())
    new_sr = dict(sr_docs)
    rep, seen_md5 = [], {}
    failed_orders = {c['order'] for cid, c in cards.items() if cid.startswith('fd-') and 'ไม่มีพัสดุตีกลับ' not in c.get('typ', '') and 'ไม่อยู่ในรายการตีกลับ' not in c.get('typ', '')}
    for _, name, kind, rows, md5 in parsed:
        st = dict(rows=0, card_rows=0, skip=collections.Counter())
        if md5 in seen_md5:
            rep.append(dict(name=name, kind=kind, label=KIND_LABEL[kind], rows=0, cards=0, new=0, changed=0, same=0, skipped={}, note='ไฟล์ซ้ำกับ "%s" อ่านครั้งเดียว' % seen_md5[md5], ok=True))
            continue
        seen_md5[md5] = name
        if kind == 'sr':
            docs = parse_sr(rows)
            bad = [s['sr'] for s in docs if s['lines'] and abs(sum(l['amt'] for l in s['lines']) - s['total']) > 0.01]
            fresh = sum(1 for s in docs if s['sr'] not in new_sr)
            for s in docs:
                new_sr[s['sr']] = s
            rep.append(dict(name=name, kind=kind, label=KIND_LABEL[kind], rows=sum(len(s['lines']) for s in docs), cards=len(docs), new=fresh,
                            changed=len(docs) - fresh, same=0, skipped={}, ok=not bad,
                            note='SR %d ใบ (ออนไลน์ %d ใบ)' % (len(docs), sum(1 for s in docs if sr_online(s))) + (' · ยอดไม่ตรงหัวเอกสาร: ' + ', '.join(bad) if bad else '')))
            continue
        if kind == 'sh_fd':
            built = build_sh_fd(rows, st)
            failed_orders |= {c['order'] for c in built.values()}
        elif kind == 'sh_cc':
            built = build_sh_cc(rows, st, failed_orders)
        elif kind == 'sh_rr':
            built = build_sh_rr(rows, st)
        elif kind == 'tt_od':
            built = build_tt_od(rows, st)
        elif kind == 'tt_rt':
            built = build_tt_rt(rows, st)
        elif kind == 'lz_od':
            built = build_lz_od(rows, st)
        else:
            built = build_lz_rt(rows, st)
        n_new = n_chg = n_same = 0
        for cid, c in built.items():
            if cid not in new_cards:
                n_new += 1
            elif new_cards[cid] != c:
                n_chg += 1
            else:
                n_same += 1
            new_cards[cid] = c
        skipped = dict(st['skip'])
        rep.append(dict(name=name, kind=kind, label=KIND_LABEL[kind], rows=st['rows'], card_rows=st['card_rows'], cards=len(built), new=n_new,
                        changed=n_chg, same=n_same, skipped=skipped, ok=st['rows'] == st['card_rows'] + sum(skipped.values())))
    sr_by_card, sr_meta = verify_sr(new_cards, staff, list(new_sr.values()))
    # duplicates across the whole set after the import
    warn = []
    def dups(label, f):
        cnt = collections.Counter(f(c) for c in new_cards.values())
        bad = sorted(k for k, v in cnt.items() if v > 1 and k and k != '-' and not str(k).startswith('('))
        if bad:
            warn.append('%s ซ้ำ: %s' % (label, ', '.join(map(str, bad[:10]))))
    dups('รหัสขอคืน', lambda c: c.get('req'))
    dups('เลขพัสดุขาไป', lambda c: c.get('out_track'))
    dups('เลขพัสดุขาคืน', lambda c: c.get('ret_track'))
    for cid, c in new_cards.items():
        if c.get('typ') not in TYPES:
            warn.append('การ์ด %s มีสถานะที่ไม่รู้จัก: %s' % (cid, c.get('typ')))
        if not c.get('order') or not c.get('items'):
            warn.append('การ์ด %s ไม่มีรหัสคำสั่งซื้อหรือรายการสินค้า' % cid)
    states = collections.Counter(v['sr_state'] for v in sr_by_card.values())
    report = dict(files=rep, unknown=unknown, warnings=warn,
                  totals=dict(before=len(cards), after=len(new_cards), new=len(new_cards) - len(cards)),
                  sr=dict(docs=len(new_sr), matched=len(sr_by_card), ok=states.get('ok', 0), diff=states.get('diff', 0), eye=states.get('eye', 0),
                          orph=len({o['order'] for o in sr_meta['orph']}), to=sr_meta['to']),
                  ok=not unknown and all(f['ok'] for f in rep))
    return new_cards, new_sr, sr_by_card, sr_meta, report


# ---------------------------------------------------------------- database (Postgres)

_con = None


def db_url():
    """Find the Postgres connection string whatever name the Vercel storage integration gave it
    (DATABASE_URL, POSTGRES_URL, or a custom prefix such as STORAGE_DATABASE_URL). Pooled URLs first."""
    for k in ('DATABASE_URL', 'POSTGRES_URL'):
        if os.environ.get(k, '').startswith('postgres'):
            return os.environ[k]
    cands = sorted((k, v) for k, v in os.environ.items() if v.startswith(('postgres://', 'postgresql://')))
    for k, v in cands:
        if 'UNPOOLED' not in k and 'NON_POOLING' not in k and 'NO_SSL' not in k:
            return v
    return cands[0][1] if cands else ''


def con():
    global _con
    if _con is not None:
        try:
            _con.run('SELECT 1')
            return _con
        except Exception:
            _con = None
    import pg8000.native
    url = db_url()
    if not url:
        raise RuntimeError('NO_DB')
    u = urlparse(url)
    c = pg8000.native.Connection(user=unquote(u.username or ''), password=unquote(u.password or ''), host=u.hostname, port=u.port or 5432,
                                 database=u.path.lstrip('/'), ssl_context=ssl.create_default_context(), timeout=20)
    c.run("CREATE TABLE IF NOT EXISTS cases (id text PRIMARY KEY, data jsonb NOT NULL, staff jsonb NOT NULL DEFAULT CAST('{}' AS jsonb), "
          "sr jsonb, updated_at timestamptz NOT NULL DEFAULT now())")
    c.run("CREATE TABLE IF NOT EXISTS meta (key text PRIMARY KEY, data jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())")
    c.run("CREATE TABLE IF NOT EXISTS log (id bigserial PRIMARY KEY, at timestamptz NOT NULL DEFAULT now(), who text, what text, detail jsonb)")
    # evidence photos (already shrunk in the browser); data is base64 text
    c.run("CREATE TABLE IF NOT EXISTS photos (id bigserial PRIMARY KEY, case_id text NOT NULL, mime text NOT NULL, data text NOT NULL, note text, by_name text, at timestamptz NOT NULL DEFAULT now())")
    _con = c
    return c


def meta_get(key, default=None):
    r = con().run('SELECT data FROM meta WHERE key=:k', k=key)
    return r[0][0] if r else default


def meta_set(key, data):
    con().run('INSERT INTO meta(key,data) VALUES(:k, CAST(:d AS jsonb)) ON CONFLICT (key) DO UPDATE SET data=EXCLUDED.data, updated_at=now()',
              k=key, d=json.dumps(data, ensure_ascii=False))


def load_all():
    rows = con().run('SELECT id, data, staff, sr FROM cases')
    cards = collections.OrderedDict((r[0], r[1]) for r in rows)
    staff = {r[0]: (r[2] or {}) for r in rows}
    sr = {r[0]: r[3] for r in rows if r[3]}
    return cards, staff, sr


def log(who, what, detail=None):
    con().run('INSERT INTO log(who,what,detail) VALUES(:w,:a,CAST(:d AS jsonb))', w=who, a=what, d=json.dumps(detail or {}, ensure_ascii=False))


# ---------------------------------------------------------------- auth: shop PIN + name, admin PIN for imports

def _hash(pin, salt):
    return hashlib.sha256((salt + ':' + str(pin)).encode()).hexdigest()


def make_token(auth, name, role):
    p = base64.urlsafe_b64encode(json.dumps({'n': name, 'r': role, 'e': int(time.time()) + 60 * 60 * 24 * 30}, ensure_ascii=False).encode()).decode()
    return p + '.' + hmac.new(auth['secret'].encode(), p.encode(), hashlib.sha256).hexdigest()


def read_token(auth, tok):
    try:
        p, sig = tok.split('.')
        if not hmac.compare_digest(sig, hmac.new(auth['secret'].encode(), p.encode(), hashlib.sha256).hexdigest()):
            return None
        d = json.loads(base64.urlsafe_b64decode(p.encode()).decode())
        return d if d.get('e', 0) > time.time() else None
    except Exception:
        return None


# ---------------------------------------------------------------- HTTP

class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n).decode() or '{}') if n else {}

    def _who(self, auth):
        h = self.headers.get('Authorization') or ''
        return read_token(auth, h[7:]) if h.startswith('Bearer ') and auth else None

    def do_GET(self):
        self._route('GET')

    def do_POST(self):
        self._route('POST')

    def _route(self, method):
        a = (parse_qs(urlparse(self.path).query).get('a') or [''])[0]
        try:
            auth = meta_get('auth')
            if a == 'state' and method == 'GET':
                if not auth:
                    return self._send(200, {'setup': True})
                who = self._who(auth)
                if not who:
                    return self._send(401, {'error': 'login'})
                cards, staff, sr = load_all()
                ph = collections.defaultdict(list)
                for r in con().run('SELECT id, case_id, note, by_name, at FROM photos ORDER BY id'):
                    ph[r[1]].append({'id': r[0], 'note': r[2] or '', 'by': r[3] or '', 'at': r[4].isoformat() if r[4] else ''})
                out = []
                for cid, c in cards.items():
                    x = dict(c)
                    x.update(sr.get(cid) or {})
                    x.update(staff.get(cid) or {})
                    x['id'] = cid
                    x['photos'] = ph.get(cid, [])
                    out.append(x)
                m = meta_get('sr') or {}
                return self._send(200, {'cases': out, 'meta': m, 'me': {'name': who['n'], 'role': who['r']}, 'imports': (meta_get('imports') or [])[-5:]})
            if a == 'photo' and method == 'GET':
                if not self._who(auth):
                    return self._send(401, {'error': 'login'})
                pid = (parse_qs(urlparse(self.path).query).get('pid') or ['0'])[0]
                r = con().run('SELECT mime, data FROM photos WHERE id=:i', i=int(pid) if pid.isdigit() else 0)
                if not r:
                    return self._send(404, {'error': 'no_photo'})
                raw = base64.b64decode(r[0][1])
                self.send_response(200)
                self.send_header('Content-Type', r[0][0])
                self.send_header('Cache-Control', 'private, max-age=86400')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if method != 'POST':
                return self._send(404, {'error': 'not_found'})
            b = self._body()
            if a == 'setup':
                if auth:
                    return self._send(409, {'error': 'already_setup'})
                sp, ap = str(b.get('staff_pin') or ''), str(b.get('admin_pin') or '')
                if len(sp) < 4 or len(ap) < 6 or sp == ap:
                    return self._send(400, {'error': 'pin_rule'})
                salt = base64.urlsafe_b64encode(os.urandom(12)).decode()
                meta_set('auth', {'salt': salt, 'staff': _hash(sp, salt), 'admin': _hash(ap, salt), 'secret': base64.urlsafe_b64encode(os.urandom(32)).decode()})
                return self._send(200, {'ok': True})
            if a == 'login':
                if not auth:
                    return self._send(409, {'error': 'setup'})
                pin, name = str(b.get('pin') or ''), str(b.get('name') or '').strip()[:30]
                role = 'admin' if hmac.compare_digest(_hash(pin, auth['salt']), auth['admin']) else ('staff' if hmac.compare_digest(_hash(pin, auth['salt']), auth['staff']) else None)
                if not role or not name:
                    time.sleep(1.2)
                    return self._send(401, {'error': 'bad_pin'})
                return self._send(200, {'token': make_token(auth, name, role), 'name': name, 'role': role})
            who = self._who(auth)
            if not who:
                return self._send(401, {'error': 'login'})
            if a == 'act':
                cid, p = str(b.get('id') or ''), b.get('patch') or {}
                now = dt.datetime.utcnow().isoformat() + 'Z'
                patch = {}
                if 'check' in p and p['check'] in ('', 'ok', 'part', 'none'):
                    patch.update(check=p['check'], checked_at=now if p['check'] else '', checked_by=who['n'] if p['check'] else '')
                if 'note' in p:  # a problem note can be written on any card at any time; clearing a check never wipes it
                    tx = str(p['note']).strip()[:500]
                    patch.update(note=tx, note_at=now if tx else '', note_by=who['n'] if tx else '')
                if 'loc' in p:  # where the returned goods were put, so the returns desk can write the SR
                    lv = str(p['loc']).strip()[:80]
                    patch.update(loc=lv, loc_at=now if lv else '', loc_by=who['n'] if lv else '')
                if 'pinned' in p:
                    patch['pinned'] = bool(p['pinned'])
                if 'action' in p and p['action'] in ('', 'dispute', 'refund_ok', 'accept'):
                    patch.update(action=p['action'], action_at=now if p['action'] else '', action_by=who['n'] if p['action'] else '')
                if 'sr_confirmed' in p and who['r'] == 'admin' and p['sr_confirmed'] in ('', 'prod'):
                    patch['sr_confirmed'] = p['sr_confirmed']
                if not patch:
                    return self._send(400, {'error': 'empty'})
                r = con().run('UPDATE cases SET staff = staff || CAST(:p AS jsonb), updated_at=now() WHERE id=:i RETURNING staff', p=json.dumps(patch, ensure_ascii=False), i=cid)
                if not r:
                    return self._send(404, {'error': 'no_card'})
                log(who['n'], 'act', {'id': cid, 'patch': patch})
                if 'sr_confirmed' in patch:
                    self._recompute_sr()
                return self._send(200, {'ok': True, 'staff': r[0][0]})
            if a == 'photo_add':
                cid, b64 = str(b.get('id') or ''), str(b.get('b64') or '')
                if len(b64) > 950000:
                    return self._send(413, {'error': 'too_big'})
                try:
                    head = base64.b64decode(b64[:24])
                except Exception:
                    head = b''
                mime = 'image/jpeg' if head[:3] == b'\xff\xd8\xff' else ('image/png' if head[:8] == b'\x89PNG\r\n\x1a\n' else ('image/webp' if head[:4] == b'RIFF' and head[8:12] == b'WEBP' else ''))
                if not mime:
                    return self._send(400, {'error': 'not_image'})
                if not con().run('SELECT 1 FROM cases WHERE id=:i', i=cid):
                    return self._send(404, {'error': 'no_card'})
                if con().run('SELECT count(*) FROM photos WHERE case_id=:i', i=cid)[0][0] >= 20:
                    return self._send(400, {'error': 'too_many'})
                r = con().run('INSERT INTO photos(case_id, mime, data, note, by_name) VALUES(:c, :m, :d, :n, :w) RETURNING id, at',
                              c=cid, m=mime, d=b64, n=str(b.get('note') or '').strip()[:200], w=who['n'])
                log(who['n'], 'photo_add', {'id': cid, 'photo': r[0][0]})
                return self._send(200, {'ok': True, 'photo': {'id': r[0][0], 'note': str(b.get('note') or '').strip()[:200], 'by': who['n'], 'at': r[0][1].isoformat() if r[0][1] else ''}})
            if a == 'photo_del':
                pid = int(b.get('pid') or 0)
                r = con().run('SELECT by_name, case_id FROM photos WHERE id=:i', i=pid)
                if not r:
                    return self._send(404, {'error': 'no_photo'})
                if who['r'] != 'admin' and r[0][0] != who['n']:
                    return self._send(403, {'error': 'not_yours'})
                con().run('DELETE FROM photos WHERE id=:i', i=pid)
                log(who['n'], 'photo_del', {'id': r[0][1], 'photo': pid})
                return self._send(200, {'ok': True})
            if who['r'] != 'admin':
                return self._send(403, {'error': 'admin_only'})
            if a == 'import':
                files = [(f.get('name') or 'file', base64.b64decode(f.get('b64') or '')) for f in (b.get('files') or [])]
                if not files:
                    return self._send(400, {'error': 'no_files'})
                cards, staff, _ = load_all()
                new_cards, new_sr, sr_by, sr_meta, rep = run_import(files, cards, staff, meta_get('sr_docs') or {})
                if not b.get('commit'):
                    return self._send(200, {'preview': rep})
                if not rep['ok']:
                    return self._send(400, {'error': 'not_ok', 'preview': rep})
                self._save(new_cards, sr_by, sr_meta, new_sr)
                hist = (meta_get('imports') or [])[-19:] + [{'at': dt.datetime.utcnow().isoformat() + 'Z', 'by': who['n'], 'files': [f['name'] for f in rep['files']],
                                                             'new': rep['totals']['new'], 'after': rep['totals']['after']}]
                meta_set('imports', hist)
                log(who['n'], 'import', {'files': [f['name'] for f in rep['files']], 'totals': rep['totals']})
                return self._send(200, {'ok': True, 'preview': rep})
            if a == 'restore':
                items = b.get('cases') or []
                if not items:
                    return self._send(400, {'error': 'empty'})
                rows = [{'id': x['id'], 'data': x['data'], 'staff': {k: v for k, v in (x.get('staff') or {}).items() if k in STAFF_KEYS}} for x in items]
                con().run("INSERT INTO cases(id,data,staff) SELECT x->>'id', x->'data', x->'staff' FROM jsonb_array_elements(CAST(:j AS jsonb)) x "
                          "ON CONFLICT (id) DO UPDATE SET data=EXCLUDED.data, staff=EXCLUDED.staff, updated_at=now()", j=json.dumps(rows, ensure_ascii=False))
                if b.get('sr_docs'):
                    meta_set('sr_docs', b['sr_docs'])
                self._recompute_sr()
                log(who['n'], 'restore', {'cards': len(rows)})
                return self._send(200, {'ok': True, 'cards': len(rows)})
            return self._send(404, {'error': 'not_found'})
        except RuntimeError as e:
            if str(e) == 'NO_DB':
                # names only, never values: helps see whether a database was connected under another name
                seen = sorted(k for k in os.environ if any(t in k.upper() for t in ('POSTGRES', 'DATABASE', 'PGHOST', 'NEON', 'SUPABASE')))
                return self._send(503, {'error': 'no_db', 'db_vars_seen': seen})
            return self._send(500, {'error': 'server', 'detail': str(e)[:200]})
        except Exception as e:
            return self._send(500, {'error': 'server', 'detail': (type(e).__name__ + ': ' + str(e))[:300]})

    def _save(self, cards, sr_by, sr_meta, sr_docs):
        c = con()
        rows = [{'id': k, 'data': v, 'sr': sr_by.get(k)} for k, v in cards.items()]
        c.run('START TRANSACTION')
        try:
            c.run("INSERT INTO cases(id,data,sr) SELECT x->>'id', x->'data', NULLIF(x->'sr', CAST('null' AS jsonb)) FROM jsonb_array_elements(CAST(:j AS jsonb)) x "
                  "ON CONFLICT (id) DO UPDATE SET data=EXCLUDED.data, sr=EXCLUDED.sr, updated_at=now()", j=json.dumps(rows, ensure_ascii=False))
            meta_set('sr', sr_meta)
            meta_set('sr_docs', sr_docs)
            c.run('COMMIT')
        except Exception:
            c.run('ROLLBACK')
            raise

    def _recompute_sr(self):
        cards, staff, _ = load_all()
        sr_docs = meta_get('sr_docs') or {}
        sr_by, sr_meta = verify_sr(cards, staff, list(sr_docs.values()))
        rows = [{'id': k, 'sr': sr_by.get(k)} for k in cards]
        con().run("UPDATE cases c SET sr = NULLIF(x->'sr', CAST('null' AS jsonb)) FROM jsonb_array_elements(CAST(:j AS jsonb)) x WHERE c.id = x->>'id'",
                  j=json.dumps(rows, ensure_ascii=False))
        meta_set('sr', sr_meta)
