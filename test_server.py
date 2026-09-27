import hashlib,http.client,io,json,os,re,tempfile,threading,unittest,zipfile
import server
import concurrent.futures,subprocess,sys
from unittest.mock import Mock

class LibraryTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.temp=tempfile.TemporaryDirectory();server.DATA=server.Path(cls.temp.name);os.environ['UPLOAD_TOKEN']='test-secret'
  cls.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);threading.Thread(target=cls.http.serve_forever,daemon=True).start()
  cls.mod=json.loads((server.PUBLIC/'catalog.json').read_text())['mods'][0]['id']
 @classmethod
 def tearDownClass(cls):cls.http.shutdown();cls.http.server_close();cls.temp.cleanup()
 def request(self,method,path,body=None,headers={},chunked=False):
  c=http.client.HTTPConnection('127.0.0.1',self.http.server_port);c.request(method,path,body,headers,encode_chunked=chunked);r=c.getresponse();result=(r.status,r.read());c.close();return result
 def test_unauthorized_upload(self):self.assertEqual(self.request('PUT','/upload/'+self.mod,b'x')[0],401)
 def test_no_source_or_path_traversal(self):
  for p in ['/server.py','/../LICENSE.txt','/download/../../server.py.zip']:
   self.assertEqual(self.request('GET',p)[0],404)
 def test_validated_upload_and_download(self):
  buf=io.BytesIO()
  with zipfile.ZipFile(buf,'w') as z:z.writestr('THUNDER-BUDDIES-LICENSE.txt','test');z.writestr('sample.c','source')
  payload=buf.getvalue();status,body=self.request('PUT','/upload/'+self.mod,iter([payload]),{'Authorization':'Bearer test-secret'},True)
  self.assertEqual(status,201);self.assertEqual(json.loads(body)['sha256'],hashlib.sha256(payload).hexdigest());self.assertEqual(self.request('GET','/download/'+self.mod+'.zip'),(200,payload))
  self.assertEqual(self.request('GET','/download/'+self.mod+'.zip',headers={'Range':'bytes=0-9'}),(206,payload[:10]))
  self.assertEqual(self.request('GET','/download/'+self.mod+'.zip',headers={'Range':'bytes=-12'}),(206,payload[-12:]))
  self.assertEqual(self.request('GET','/download/'+self.mod+'.zip',headers={'Range':'bytes=999999999-'} )[0],416)
 def test_removed_archive_cannot_be_downloaded(self):
  slug='samplemod-newcar-b17ac56f'
  (server.DATA/(slug+'.zip')).write_bytes(b'previously uploaded archive')
  (server.DATA/(slug+'.json')).write_text('{}')
  self.assertEqual(self.request('GET','/download/'+slug+'.zip')[0],404)
  self.assertEqual(self.request('HEAD','/download/'+slug+'.zip')[0],404)
  self.assertEqual(self.request('GET','/download/'+slug+'.zip',headers={'Range':'bytes=0-9'})[0],404)
 def test_persistent_atomic_counts(self):
  before=server.download_counts().get('counter-test',0)
  with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
   list(pool.map(lambda _:server.record_download('counter-test'),range(40)))
  self.assertEqual(server.download_counts()['counter-test'],before+40)
  env=dict(os.environ,DATA_DIR=str(server.DATA))
  result=subprocess.check_output([sys.executable,'-c',"import server;print(server.download_counts()['counter-test'])"],env=env,text=True)
  self.assertEqual(int(result.strip()),before+40)
 def test_only_completed_full_transfer_counted(self):
  slug=self.mod;data=b'test download bytes'
  (server.DATA/(slug+'.zip')).write_bytes(data);(server.DATA/(slug+'.json')).write_text('{}')
  before=server.download_counts().get(slug,0)
  self.assertEqual(self.request('HEAD','/download/'+slug+'.zip')[0],200)
  self.assertEqual(self.request('GET','/download/'+slug+'.zip',headers={'Range':'bytes=0-3'})[0],206)
  handler=server.Handler.__new__(server.Handler);handler.headers={};handler.path='/download/'+slug+'.zip';handler.command='GET'
  handler.send_response=Mock();handler.send_header=Mock();handler.end_headers=Mock();handler.wfile=Mock();handler.wfile.write.side_effect=BrokenPipeError
  with self.assertRaises(BrokenPipeError):handler.do_GET()
  self.assertEqual(server.download_counts().get(slug,0),before)
  self.assertEqual(self.request('GET','/download/'+slug+'.zip'),(200,data))
  # Request completion can precede the server-side SQLite commit by a few milliseconds.
  import time
  for _ in range(100):
   if server.download_counts().get(slug,0)==before+1:break
   time.sleep(.01)
  self.assertEqual(server.download_counts()[slug],before+1)
  catalog=json.loads(self.request('GET','/api/catalog')[1]);entry=next(m for m in catalog['mods'] if m['id']==slug)
  self.assertEqual(entry['downloadCount'],before+1)
 def test_invalid_zip_not_published(self):
  status,_=self.request('PUT','/upload/'+self.mod,iter([b'bad']),{'Authorization':'Bearer test-secret'},True);self.assertEqual(status,400)
 def test_catalog_no_local_paths(self):
  status,body=self.request('GET','/api/catalog');self.assertEqual(status,200);self.assertFalse(b'C:\\\\Users' in body, 'Personal Windows path exposed');self.assertFalse(b'llkoo' in body, 'Local account name exposed');self.assertFalse(b'uploadToken' in body, 'Upload credential exposed')
 def test_public_metadata_and_assets(self):
  status,body=self.request('GET','/api/catalog');mods=json.loads(body)['mods'];self.assertEqual(len({m['id'] for m in mods}),len(mods))
  for mod in mods:
   self.assertRegex(mod['projectId'],r'^[A-F0-9]{16}$')
   if mod.get('workshopUrl'):
    self.assertTrue(mod.get('workshopEvidence'));self.assertTrue(mod.get('workshopTitle'));self.assertIn(mod['workshopId'],mod['workshopUrl'])
   else:self.assertIsNone(mod.get('workshopId'))
   for dep in mod['dependencies']:
    self.assertRegex(dep['id'],r'^[A-F0-9]{16}$');self.assertTrue(dep['name'])
   if mod.get('thumbnail'):self.assertEqual(self.request('HEAD',mod['thumbnail'])[0],200)
  self.assertEqual(self.request('GET','/license')[0],200)
  self.assertIn(b'The community before the platform',self.request('GET','/story')[1])
  self.assertEqual(self.request('GET','/license.txt')[1],(server.PUBLIC.parent/'LICENSE.txt').read_bytes())

if __name__=='__main__':unittest.main()


