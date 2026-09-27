import hashlib, hmac, json, os, re, threading, zipfile, sqlite3, logging
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

DATA = Path(os.environ.get('DATA_DIR', '/data'))
PUBLIC = Path(__file__).parent / 'public'
LOCK = threading.Lock()
MAX_UPLOAD = 4 * 1024**3

def counts_connection():
    DATA.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA / 'download-counts.sqlite3', timeout=15)
    db.execute('CREATE TABLE IF NOT EXISTS downloads (slug TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0)')
    return db

def download_counts():
    db = counts_connection()
    try:
        return dict(db.execute('SELECT slug, count FROM downloads'))
    finally:
        db.close()

def record_download(slug):
    db = counts_connection()
    try:
        with db:
            db.execute('INSERT INTO downloads(slug,count) VALUES (?,1) ON CONFLICT(slug) DO UPDATE SET count=count+1', (slug,))
    finally:
        db.close()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def respond(self, code, body=b'', content_type='application/json'):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        if self.command != 'HEAD': self.wfile.write(body)
    def do_HEAD(self): self.do_GET()
    def do_GET(self):
        if self.headers.get('Host','').split(':')[0].lower() == 'osam.thunderlink.online':
            self.send_response(308)
            self.send_header('Location','https://osal.thunderlink.online'+self.path)
            self.send_header('Content-Length','0')
            self.end_headers()
            return
        path = urlsplit(self.path).path
        if path == '/health': return self.respond(200, b'{"ok":true}')
        if re.fullmatch(r'/thumbnails/[a-z0-9-]+\.(png|jpg|jpeg)',path):
            thumbnail=PUBLIC/path.lstrip('/')
            if not thumbnail.is_file():return self.respond(404,b'{}')
            return self.respond(200,thumbnail.read_bytes(),'image/png' if path.endswith('.png') else 'image/jpeg')
        if path == '/api/catalog':
            catalog = json.loads((PUBLIC/'catalog.json').read_text())
            counts = download_counts()
            catalog['downloadCountNote'] = 'Completed full-file HTTP transfers since tracking began. Partial/resumed transfers and HEAD checks are excluded. Not unique people or confirmed saves to disk; earlier downloads were not recorded.'
            for item in catalog['mods']:
                metadata = DATA / (item['id']+'.json')
                item['available'] = metadata.exists() and (DATA/(item['id']+'.zip')).exists()
                if item['available']: item.update(json.loads(metadata.read_text()))
                item['downloadCount'] = counts.get(item['id'], 0)
            return self.respond(200, json.dumps(catalog).encode())
        match = re.fullmatch(r'/download/([a-z0-9-]{1,100})\.zip', path)
        if match:
            published_ids = {m['id'] for m in json.loads((PUBLIC/'catalog.json').read_text())['mods']}
            if match[1] not in published_ids: return self.respond(404,b'{"error":"Not published"}')
            file = DATA / (match[1]+'.zip')
            if not file.is_file() or not (DATA/(match[1]+'.json')).is_file(): return self.respond(404,b'{"error":"Not published"}')
            length=file.stat().st_size; start=0; end=length-1; status=200
            requested=self.headers.get('Range')
            if requested:
                bounds=re.fullmatch(r'bytes=(\d*)-(\d*)',requested)
                if not bounds or not any(bounds.groups()): return self.respond(416,b'{}')
                if bounds[1]:
                    start=int(bounds[1]);end=min(int(bounds[2]),length-1) if bounds[2] else length-1
                else:
                    start=max(0,length-int(bounds[2]));end=length-1
                if start>=length or end<start: return self.respond(416,b'{}')
                status=206
            self.send_response(status)
            self.send_header('Content-Type','application/zip')
            self.send_header('Content-Disposition',f'attachment; filename="{file.name}"')
            self.send_header('Accept-Ranges','bytes')
            self.send_header('Cache-Control','no-store')
            if status==206:self.send_header('Content-Range',f'bytes {start}-{end}/{length}')
            self.send_header('Content-Length',str(end-start+1))
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            if self.command != 'HEAD':
                with file.open('rb') as source:
                    source.seek(start);remaining=end-start+1
                    while remaining:
                        block=source.read(min(1024*1024,remaining))
                        if not block:break
                        self.wfile.write(block);remaining-=len(block)
                self.wfile.flush()
                if remaining == 0 and status == 200:
                    try: record_download(match[1])
                    except sqlite3.Error: logging.exception('Download count could not be persisted')
            return
        mapping={'/credits':('credits.html','text/html; charset=utf-8'),'/credits.js':('credits.js','text/javascript'),'/story':('story.html','text/html; charset=utf-8'),'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript'),'/style.css':('style.css','text/css'),'/license':('license.html','text/html; charset=utf-8'),'/license.txt':('../LICENSE.txt','text/plain; charset=utf-8')}
        if path not in mapping: return self.respond(404,b'{}')
        name, mime = mapping[path]
        return self.respond(200,(PUBLIC/name).read_bytes(),mime)
    def do_PUT(self):
        token=os.environ.get('UPLOAD_TOKEN','')
        if not token or not hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+token):
            self.close_connection=True
            return self.respond(401,b'{}')
        match=re.fullmatch(r'/upload/([a-z0-9-]{1,100})',urlsplit(self.path).path)
        catalog=json.loads((PUBLIC/'catalog.json').read_text())
        if not match or match[1] not in {m['id'] for m in catalog['mods']}:
            self.close_connection=True
            return self.respond(404,b'{}')
        if self.headers.get('Transfer-Encoding','').lower()!='chunked':
            self.close_connection=True
            return self.respond(411,b'{}')
        if not LOCK.acquire(blocking=False):
            self.close_connection=True
            return self.respond(409,b'{}')
        temp=DATA/(match[1]+'.part')
        try:
            DATA.mkdir(parents=True,exist_ok=True)
            digest=hashlib.sha256(); total=0
            with temp.open('wb') as output:
                while True:
                    line=self.rfile.readline(128)
                    size=int(line.strip(),16)
                    if size==0:
                        if self.rfile.read(2)!=b'\r\n': raise ValueError('Invalid terminator')
                        break
                    if size>2*1024*1024 or total+size>MAX_UPLOAD: raise ValueError('Upload limit')
                    chunk=self.rfile.read(size)
                    if len(chunk)!=size or self.rfile.read(2)!=b'\r\n': raise ValueError('Incomplete chunk')
                    output.write(chunk);digest.update(chunk);total+=size
            with zipfile.ZipFile(temp) as archive:
                if len(archive.infolist())>200000 or sum(i.file_size for i in archive.infolist())>12*1024**3: raise ValueError('Expanded archive limit')
                if archive.testzip() is not None: raise ValueError('ZIP integrity failed')
                if 'THUNDER-BUDDIES-LICENSE.txt' not in archive.namelist(): raise ValueError('Missing license')
            info={'sha256':digest.hexdigest(),'downloadBytes':total}
            temp.replace(DATA/(match[1]+'.zip'))
            metadata=DATA/(match[1]+'.json.tmp'); metadata.write_text(json.dumps(info));metadata.replace(DATA/(match[1]+'.json'))
            self.respond(201,json.dumps(info).encode())
        except (ValueError, OSError, zipfile.BadZipFile):
            temp.unlink(missing_ok=True);self.close_connection=True;self.respond(400,b'{"error":"Upload validation failed"}')
        finally: LOCK.release()

if __name__=='__main__':
    ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),Handler).serve_forever()
