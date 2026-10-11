# -*- coding: utf-8 -*-
# Server rules, run against an in-memory stand-in for Postgres. Synthetic cards only - never real orders.
#   python3 tests/test_api.py
import sys, os, io, json, base64, datetime
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'api'))
import index as app


class Fake:
    def __init__(s): s.cases = {}; s.meta = {}; s.log = []; s.photos = {}; s.pid = 0
    def run(s, q, **k):
        q = ' '.join(q.split())
        if q.startswith("SELECT data->>'typ', staff->>'closed'"):
            x = s.cases.get(k['i']); return [[x['data'].get('typ'), x['staff'].get('closed'), x['staff'].get('closed_typ')]] if x else []
        if q.startswith('SELECT 1 FROM cases'): return [[1]] if k['i'] in s.cases else []
        if q.startswith('SELECT 1') or q.startswith('CREATE') or q in ('START TRANSACTION', 'COMMIT', 'ROLLBACK'): return []
        if q.startswith('SELECT data FROM meta'): return [[s.meta[k['k']]]] if k['k'] in s.meta else []
        if q.startswith('INSERT INTO meta'): s.meta[k['k']] = json.loads(k['d']); return []
        if q.startswith('SELECT id, data, staff, sr FROM cases'): return [[i, c['data'], c['staff'], c['sr']] for i, c in s.cases.items()]
        if q.startswith('INSERT INTO log'): s.log.append((k['w'], k['a'])); return []
        if q.startswith('UPDATE cases SET staff'):
            if k['i'] not in s.cases: return []
            s.cases[k['i']]['staff'].update(json.loads(k['p'])); return [[s.cases[k['i']]['staff']]]
        if q.startswith('INSERT INTO cases(id,data,staff)'):
            for x in json.loads(k['j']): s.cases[x['id']] = {'data': x['data'], 'staff': x['staff'], 'sr': None}
            return []
        if q.startswith('INSERT INTO cases(id,data,sr)'):
            for x in json.loads(k['j']): s.cases.setdefault(x['id'], {'staff': {}})['data'] = x['data']; s.cases[x['id']]['sr'] = x['sr']
            return []
        if q.startswith('UPDATE cases c SET sr'): return []   # tests set SR results by hand
        if q.startswith('SELECT id, case_id, note, by_name, at FROM photos'): return [[i, x['c'], x['n'], x['w'], x['at']] for i, x in sorted(s.photos.items())]
        if q.startswith('SELECT mime, data FROM photos'): return [[s.photos[k['i']]['m'], s.photos[k['i']]['d']]] if k['i'] in s.photos else []
        if q.startswith('SELECT count(*) FROM photos'): return [[sum(1 for x in s.photos.values() if x['c'] == k['i'])]]
        if q.startswith('INSERT INTO photos'):
            s.pid += 1; s.photos[s.pid] = dict(c=k['c'], m=k['m'], d=k['d'], n=k['n'], w=k['w'], at=datetime.datetime(2026, 10, 8, 9, 0, 0)); return [[s.pid, s.photos[s.pid]['at']]]
        if q.startswith('SELECT by_name, case_id FROM photos'): return [[s.photos[k['i']]['w'], s.photos[k['i']]['c']]] if k['i'] in s.photos else []
        if q.startswith('DELETE FROM photos'): s.photos.pop(k['i'], None); return []
        raise AssertionError('unhandled SQL: ' + q[:80])


fake = Fake(); app._con = fake; app.time.sleep = lambda x: None
app.verify_sr = lambda cards, staff, docs: ({}, {})


def call(method, a, body=None, token=None):
    h = app.handler.__new__(app.handler); raw = json.dumps(body).encode() if body is not None else b''
    h.path = '/api?a=' + a; h.headers = {'Content-Length': str(len(raw))}
    if token: h.headers['Authorization'] = 'Bearer ' + token
    h.rfile = io.BytesIO(raw); h.wfile = io.BytesIO(); st = {}
    h.send_response = lambda c: st.__setitem__('c', c); h.send_header = lambda *x: st.__setitem__(x[0], x[1]); h.end_headers = lambda: None
    h._route(method); out = h.wfile.getvalue()
    return st['c'], (out if st.get('Content-Type', '').startswith('image/') else json.loads(out.decode()))


N = {'pass': 0, 'fail': 0}
def ok(name, cond):
    N['pass' if cond else 'fail'] += 1; print(('PASS ' if cond else 'FAIL ') + name)


def card(i, rank, n_items, typ='ตีกลับ - ของถึงร้านแล้ว', req='-'):
    return {'id': i, 'data': {'platform': 'Shopee', 'rank': rank, 'typ': typ, 'order': 'TEST' + i, 'req': req,
                              'items': [{'name': 'สินค้า %d' % k, 'var': 'V%d' % k, 'qty': 1} for k in range(n_items)]}, 'staff': {}}


# ---- sign-in
c, j = call('GET', 'state'); ok('first run asks for setup', j.get('setup') is True)
c, j = call('POST', 'setup', {'staff_pin': '12', 'admin_pin': 'adminpin'}); ok('short staff PIN refused', c == 400)
c, j = call('POST', 'setup', {'staff_pin': '4821', 'admin_pin': 'admin-7733'}); ok('setup', c == 200)
c, j = call('POST', 'setup', {'staff_pin': '1111', 'admin_pin': 'zzzzzz'}); ok('setup cannot run twice', c == 409)
c, j = call('POST', 'login', {'pin': '9999', 'name': 'A'}); ok('wrong PIN refused', c == 401)
c, j = call('POST', 'login', {'pin': '4821', 'name': 'นิด'}); ok('staff login', c == 200 and j['role'] == 'staff'); T = j['token']
c, j = call('POST', 'login', {'pin': 'admin-7733', 'name': 'LOVE'}); ok('admin login', c == 200 and j['role'] == 'admin'); A = j['token']
c, j = call('GET', 'state'); ok('cards need login', c == 401)

# ---- cards
cards = [card('a1', 1, 3), card('a2', 1, 1), card('a3', 1, 2), card('a4', 4, 1, typ='คืนเงินอย่างเดียว - ไม่มีของกลับ', req='R1'), card('a5', 1, 1), card('a6', 1, 2)]
c, j = call('POST', 'restore', {'cases': cards}, token=T); ok('staff cannot restore', c == 403)
c, j = call('POST', 'restore', {'cases': cards}, token=A); ok('restore', c == 200 and j['cards'] == 6)
for i in ('a1', 'a2', 'a3', 'a6'): fake.cases[i]['sr'] = {'sr': 'SR-' + i, 'sr_state': 'ok'}
def S(i): return fake.cases[i]['staff']

# ---- check, location per item, notes
c, j = call('POST', 'act', {'id': 'a1', 'patch': {'check': 'ok', 'checked_by': 'ปลอม', 'loc': 'x', 'loc_items': ['E22', 'E23', 'H87']}}, token=T)
ok('result + per-item location saved in one tap, name set by server', c == 200 and S('a1')['check'] == 'ok' and S('a1')['checked_by'] == 'นิด' and S('a1')['loc_items'] == ['E22', 'E23', 'H87'] and S('a1')['loc_by'] == 'นิด')
c, j = call('POST', 'act', {'id': 'a1', 'patch': {'check': 'maybe'}}, token=T); ok('unknown result refused', c == 400)
c, j = call('POST', 'act', {'id': 'zz', 'patch': {'check': 'ok'}}, token=T); ok('unknown card refused', c == 404)
c, j = call('POST', 'act', {'id': 'a1', 'patch': {'note': '  กล่องบุบ '}}, token=T); ok('note saved with who/when', S('a1')['note'] == 'กล่องบุบ' and S('a1')['note_by'] == 'นิด' and S('a1')['note_at'])
c, j = call('POST', 'act', {'id': 'a1', 'patch': {'sr_confirmed': 'prod'}}, token=T); ok('staff cannot override SR', c == 400)

# ---- photos
JPG = base64.b64encode(b'\xff\xd8\xff\xe0' + b'x' * 5000).decode()
c, j = call('POST', 'photo_add', {'id': 'a1', 'b64': JPG, 'note': 'ใบเอกสาร'}, token=T); ok('photo upload', c == 200 and j['photo']['by'] == 'นิด'); P1 = j['photo']['id']
c, j = call('POST', 'photo_add', {'id': 'a1', 'b64': base64.b64encode(b'MZ not a picture').decode()}, token=T); ok('non-image refused', c == 400)
c, j = call('POST', 'photo_add', {'id': 'a1', 'b64': 'A' * 960000}, token=T); ok('oversized photo refused', c == 413)
c, j = call('POST', 'photo_add', {'id': 'a1', 'b64': JPG}); ok('photo upload needs login', c == 401)
c, raw = call('GET', 'photo&pid=%d' % P1, token=T); ok('photo comes back byte-for-byte', c == 200 and raw == base64.b64decode(JPG))
c, j = call('GET', 'photo&pid=%d' % P1); ok('photo needs login', c == 401)
c, j = call('GET', 'state', token=T); x = [y for y in j['cases'] if y['id'] == 'a1'][0]
ok('card list carries photo info but no image data', len(x['photos']) == 1 and JPG[:40] not in json.dumps(j) and len(j['cases']) == 6)
c, j = call('POST', 'photo_del', {'pid': P1}, token=A); ok('admin deletes photo', c == 200 and not fake.photos)

# ---- closing a clean card
c, j = call('POST', 'act', {'id': 'a5', 'patch': {'closed': 'goods'}}, token=A); ok('cannot close before the warehouse checked', c == 400 and j['error'] == 'need_check')
call('POST', 'act', {'id': 'a5', 'patch': {'check': 'ok', 'loc': 'E1', 'loc_items': ['E1']}}, token=T)
c, j = call('POST', 'act', {'id': 'a5', 'patch': {'closed': 'goods'}}, token=A); ok('goods back but no SR: cannot close', c == 400 and j['error'] == 'need_sr')
c, j = call('POST', 'act', {'id': 'a1', 'patch': {'closed': 'goods'}}, token=T); ok('staff cannot close', c == 403)
c, j = call('POST', 'act', {'id': 'a1', 'patch': {'closed': 'win'}}, token=A); ok('unknown outcome refused', c == 400)
c, j = call('POST', 'act', {'id': 'a1', 'patch': {'closed': 'goods'}}, token=A); ok('clean card closes; who/when/type set by server', c == 200 and S('a1')['closed'] == 'goods' and S('a1')['closed_by'] == 'LOVE' and S('a1')['closed_typ'] == 'ตีกลับ - ของถึงร้านแล้ว')
c, j = call('POST', 'act', {'id': 'a1', 'patch': {'check': 'none'}}, token=T); ok('closed card is locked', c == 409 and j['error'] == 'closed')
c, j = call('POST', 'act', {'id': 'a1', 'patch': {'item_issue': ['', '', {'k': 'ncr', 'n': 'x'}]}}, token=T); ok('closed card: item problems locked too', c == 409)
c, j = call('POST', 'act', {'id': 'a1', 'patch': {'closed': ''}}, token=T); ok('staff cannot reopen', c == 403)
c, j = call('POST', 'act', {'id': 'a1', 'patch': {'closed': ''}}, token=A); ok('admin reopens, result kept', c == 200 and S('a1')['closed'] == '' and S('a1')['check'] == 'ok')
c, j = call('POST', 'act', {'id': 'a4', 'patch': {'closed': 'none'}}, token=A); ok('no-goods card can be closed', c == 200)

# ---- location completeness
call('POST', 'act', {'id': 'a3', 'patch': {'check': 'ok', 'loc': 'x', 'loc_items': ['E1', '']}}, token=T)
c, j = call('POST', 'act', {'id': 'a3', 'patch': {'closed': 'goods'}}, token=A); ok('an item without location: close needs a reason', c == 400 and j['error'] == 'need_why')
c, j = call('POST', 'act', {'id': 'a3', 'patch': {'closed': 'goods', 'closed_why': 'ของอยู่หน้าร้าน'}}, token=A); ok('closes with a reason', c == 200)

# ---- problems: item flags + problem stage
c, j = call('POST', 'act', {'id': 'a6', 'patch': {'check': 'part', 'loc': 'x', 'loc_items': ['E1', 'ไม่ได้ของ'],
            'item_issue': [{'k': 'fix', 'n': 'กล่องแตก ' + 'ก' * 300, 'hack': 1}, {'k': 'bogus', 'n': 'x'}], 'prob_ok': True}}, token=T)
ok('staff cannot mark a problem as dealt with', c == 403)
c, j = call('POST', 'act', {'id': 'a6', 'patch': {'check': 'part', 'loc': 'x', 'loc_items': ['E1', 'ไม่ได้ของ'],
            'item_issue': [{'k': 'fix', 'n': 'กล่องแตก ' + 'ก' * 300, 'hack': 1}, {'k': 'bogus', 'n': 'x'}]}}, token=T)
ii = S('a6').get('item_issue')
ok('item problems saved, cleaned (unknown kind dropped, note cut, extra keys dropped)', c == 200 and ii[0]['k'] == 'fix' and len(ii[0]['n']) == 200 and set(ii[0]) == {'k', 'n'} and ii[1] == '')
c, j = call('POST', 'act', {'id': 'a6', 'patch': {'closed': 'loss', 'closed_why': 'x'}}, token=A); ok('problem card cannot be closed before it is dealt with', c == 400 and j['error'] == 'need_prob')
c, j = call('POST', 'act', {'id': 'a6', 'patch': {'prob_ok': True}}, token=A); ok('admin marks problem dealt with (who/when by server)', c == 200 and S('a6')['prob_ok'] is True and S('a6')['prob_by'] == 'LOVE' and S('a6')['prob_at'])
c, j = call('POST', 'act', {'id': 'a6', 'patch': {'check': 'none'}}, token=T); ok('changing the result re-opens the problem', c == 200 and S('a6')['prob_ok'] is False)
c, j = call('POST', 'act', {'id': 'a6', 'patch': {'closed': 'loss', 'closed_why': 'x'}}, token=A); ok('...so it cannot be closed again until dealt with', c == 400 and j['error'] == 'need_prob')
call('POST', 'act', {'id': 'a6', 'patch': {'prob_ok': True}}, token=A)
c, j = call('POST', 'act', {'id': 'a6', 'patch': {'closed': 'loss', 'closed_why': 'แพ้ข้อพิพาท'}}, token=A); ok('dealt-with problem card closes', c == 200 and S('a6')['closed'] == 'loss')
call('POST', 'act', {'id': 'a2', 'patch': {'check': 'ok', 'loc': 'E1', 'loc_items': ['E1'], 'item_issue': [{'k': 'short', 'n': 'ได้ 1'}]}}, token=T)
c, j = call('POST', 'act', {'id': 'a2', 'patch': {'closed': 'goods'}}, token=A); ok('item problem alone also blocks closing', c == 400 and j['error'] == 'need_prob')

# ---- PINs
c, j = call('POST', 'pins', {'admin_pin': 'admin-7733', 'new_staff': '5555'}, token=T); ok('staff cannot change PINs', c == 403)
c, j = call('POST', 'pins', {'admin_pin': 'wrong', 'new_staff': '5555'}, token=A); ok('change PIN needs the current admin PIN', c == 401)
c, j = call('POST', 'pins', {'admin_pin': 'admin-7733', 'new_staff': 'admin-7733'}, token=A); ok('staff PIN equal to admin PIN refused', c == 400)
c, j = call('POST', 'pins', {'admin_pin': 'admin-7733', 'new_staff': '5555'}, token=A); ok('staff PIN changed', c == 200); A2 = j['token']
c, j = call('POST', 'login', {'pin': '4821', 'name': 'นิด'}); ok('old staff PIN no longer works', c == 401)
c, j = call('GET', 'state', token=T); ok('phones on the old PIN are signed out', c == 401)
c, j = call('GET', 'state', token=A2); ok('admin stays signed in, all cards intact', c == 200 and len(j['cases']) == 6)

print('\n%d passed, %d failed' % (N['pass'], N['fail']))
sys.exit(1 if N['fail'] else 0)
