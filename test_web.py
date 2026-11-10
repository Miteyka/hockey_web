import io
import tempfile
import unittest
from urllib.parse import urlencode
import app

class WebTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.old=app.DB;app.DB=self.tmp.name+'/test.sqlite3';self.web=app.WebApplication();self.cookie=''
 def tearDown(self):app.DB=self.old;self.tmp.cleanup()
 def request(self,path='/',data=None):
  captured={};body=urlencode(data or {}).encode()
  env={'PATH_INFO':path,'REQUEST_METHOD':'POST' if data is not None else 'GET','wsgi.input':io.BytesIO(body),'CONTENT_LENGTH':str(len(body)),'HTTP_COOKIE':self.cookie}
  def start(status,headers):captured.update(status=status,headers=dict(headers))
  html=b''.join(self.web(env,start)).decode()
  self.assertNotEqual(captured['status'],'500 Internal Server Error',html)
  return captured,html
 def test_full_scenario(self):
  r,_=self.request('/login',{'role':'coach','password':app.PASSWORD});self.cookie=r['headers']['Set-Cookie'].split(';')[0]
  csrf=app.TOKENS[self.cookie.split('=')[1]][1]
  self.request('/create',{'csrf':csrf,'day':'2026-11-10','kind':'ice'})
  repo=app.SQLiteRepository(app.DB);sid=repo.db.execute('SELECT id FROM sessions').fetchone()[0];repo.db.close()
  r,_=self.request('/approve',dict(csrf=csrf,sid=sid,version=0,window=60,m0=10,m1=25,m2=20,m3=5,p1='on'));self.assertEqual(r['status'],'303 See Other')
  self.request('/start',dict(csrf=csrf,sid=sid,version=1))
  _,html=self.request();self.assertIn('Закрыть занятие',html)
  payload=dict(csrf=csrf,sid=sid,revision=0,key='test',ap1='present')
  for i,m in enumerate((10,25,20,5)):payload.update({f's{i}':'done',f'f{i}':m})
  r,_=self.request('/close',payload);self.assertEqual(r['status'],'303 See Other')
  _,html=self.request('/report');self.assertIn('100.0%',html)
  _,html=self.request('/sandbox',dict(csrf=csrf,m0=10,m1=35,m2=20,m3=5));self.assertIn('Превышение',html)
 def test_csrf(self):
  r,_=self.request('/login',{'role':'coach','password':app.PASSWORD});self.cookie=r['headers']['Set-Cookie'].split(';')[0]
  r,_=self.request('/create',{'csrf':'wrong'});self.assertEqual(r['status'],'400 Bad Request')
if __name__=='__main__':unittest.main()
