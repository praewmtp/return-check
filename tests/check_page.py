# -*- coding: utf-8 -*-
# Make sure the page script at least parses before it is shipped (macOS: uses the built-in JavaScript engine).
#   python3 tests/check_page.py
import re, os, sys, subprocess, tempfile, json
here = os.path.dirname(os.path.abspath(__file__))
html = open(os.path.join(here, '..', 'public', 'index.html'), encoding='utf-8').read()
js = re.findall(r'<script>(.*?)</script>', html, re.S)[-1]
with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as f:
    f.write(js); path = f.name
probe = "ObjC.import('Foundation');var s=$.NSString.stringWithContentsOfFileEncodingError(%s,4,null).js;try{new Function(s);'PARSE OK'}catch(e){var L=s.split('\\n');'ERR '+e+' line '+e.line+': '+(L[e.line-1]||'').slice(0,200)}" % json.dumps(path)
out = subprocess.run(['osascript', '-l', 'JavaScript', '-e', probe], capture_output=True, text=True).stdout.strip()
os.unlink(path); print(out); sys.exit(0 if out == 'PARSE OK' else 1)
