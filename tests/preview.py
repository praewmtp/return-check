# -*- coding: utf-8 -*-
# Build a local copy of the page with a pretend server and sample cards, to try the screen without touching real data.
#   python3 tests/preview.py /tmp/out   ->  writes /tmp/out/index.html ; serve it with: python3 -m http.server -d /tmp/out 8765
import sys, os, json
here = os.path.dirname(os.path.abspath(__file__))
h = open(os.path.join(here, '..', 'public', 'index.html'), encoding='utf-8').read()
cases = [
 {"id": "rr-TEST1", "platform": "Shopee", "rank": 1, "urg": "ด่วน", "typ": "ขอคืนสินค้า - ของถึงร้านแล้ว", "order": "TESTORDER0001", "req": "TESTREQ0001", "req_date": "2026-10-05", "date": "2026-10-05",
  "out_track": "TH000111", "ret_track": "TH000222", "status": "test", "refund_amt": 120, "photos": [],
  "items": [{"name": "ด้ายเย็บผ้า", "var": "A1307 ดำ", "qty": 2}, {"name": "สายพานจักร", "var": "M36", "qty": 1}, {"name": "เข็มจักร", "var": "เบอร์ 14", "qty": 3}],
  "sr": "SR6910-001", "sr_state": "ok", "sr_date": "06/10/69", "sr_chk": {"prod": "ok", "qty": "ok", "price": "ok"}},
 {"id": "fd-TEST2", "platform": "TikTok", "rank": 2, "urg": "ตีกลับ", "typ": "ตีกลับ - ของถึงร้านแล้ว", "order": "TESTORDER0002", "req": "-", "cancel_date": "2026-10-04", "date": "2026-10-04",
  "out_track": "JT000333", "status": "test", "photos": [], "items": [{"name": "กรรไกร", "var": "-", "qty": 1}], "sr": "SR6910-002", "sr_state": "ok", "sr_date": "06/10/69"},
 {"id": "fd-TEST3", "platform": "Lazada", "rank": 2, "urg": "ตีกลับ", "typ": "ตีกลับ - ของถึงร้านแล้ว", "order": "TESTORDER0003", "req": "-", "cancel_date": "2026-10-03", "date": "2026-10-03",
  "out_track": "LZ000444", "status": "test", "photos": [], "items": [{"name": "เทปตีนตุ๊กแก", "var": "ดำ 1 นิ้ว", "qty": 4}, {"name": "ซิป", "var": "ขาว 20 ซม.", "qty": 10}]}]
role = sys.argv[2] if len(sys.argv) > 2 else 'admin'
stub = "<script>window.__calls=[];window.__pid=0;var __S=" + json.dumps({"me": {"name": "LOVE", "role": role}, "cases": cases, "meta": {}, "imports": []}, ensure_ascii=False) + """;
window.fetch=function(u,o){var a=u.split('a=')[1],b=o&&o.body?JSON.parse(o.body):null;__calls.push([a,b]);
 var r=a==='state'?__S:a==='act'?(function(){var c=__S.cases.filter(function(x){return x.id===b.id})[0];for(var k in b.patch)c[k]=b.patch[k];
   ['check:checked','loc:loc','note:note','prob_ok:prob','closed:closed'].forEach(function(p){p=p.split(':');if(p[0] in b.patch)c[p[1]+'_by']=__S.me.name});return {ok:true,staff:JSON.parse(JSON.stringify(c))}})():
   a==='photo_add'?{ok:true,photo:{id:++window.__pid,note:b.note,by:__S.me.name,at:new Date().toISOString()}}:
   a==='create'?(function(){if(__S.cases.some(function(x){return x.order===b.ref&&x.platform===b.cat}))return {error:'dup'};__S.cases.push({id:'mn-'+b.ref,platform:b.cat,rank:2,urg:'สร้างเอง',typ:b.cat==='ภายใน'?'ภายใน - ลดหนี้คืนของ':'ย้ายคลัง - รับของเข้า',order:b.ref,req:'-',manual:true,cust:b.cust,date:b.date,cancel_date:b.date,status:'สร้างเอง',photos:[],items:b.items.map(function(i){return {name:i.name,var:'-',qty:i.qty}})});return {ok:true}})():{error:'x'};
 return Promise.resolve({status:200,ok:true,json:function(){return Promise.resolve(JSON.parse(JSON.stringify(r)))}})};</script>"""
i = h.rindex('<script>')
os.makedirs(sys.argv[1], exist_ok=True)
open(os.path.join(sys.argv[1], 'index.html'), 'w', encoding='utf-8').write(h[:i] + stub + h[i:])
print('preview written')
