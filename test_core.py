import unittest
from core import *

class AlgorithmsTest(unittest.TestCase):
    def setUp(self):
        self.r=SQLiteRepository(':memory:'); self.s=SessionService(self.r)
        self.a=Actor('coach',frozenset({'team'}))
        self.s.create(self.a,'s','team','2026-11-10')
        self.items=[dict(id=str(i),exercise_id='e',minutes=m) for i,m in enumerate((20,30,10))]
        self.catalog={'e':dict(name='Упражнение',category='Техника',unit='reps')}
    def tearDown(self): self.r.db.close()
    def approve(self):
        return self.s.approve(self.a,'s',0,['p'],self.items,60,{'p':'team'},self.catalog)
    def close(self,unknown=True,key='k'):
        self.approve();self.s.start(self.a,'s',1)
        self.executions=[dict(item_id='0',status='done',minutes=20),dict(item_id='1',status='partial',minutes=24),
                         dict(item_id='2',status='unknown' if unknown else 'not_done',minutes=None if unknown else 0,reason='Не измерено')]
        return self.s.close(self.a,'s',0,key,self.executions,[dict(player_id='p',status='present')])
    def report(self): return ReportService(self.r).build(self.a,'team','2026-11-01','2026-11-30')
    def test_missing(self):
        self.close(); x=self.report();self.assertEqual(x['ratio'],88);self.assertAlmostEqual(x['completeness'],200/3)
        self.assertTrue(x['details'][1]['signal'])
    def test_zero(self):
        self.close(False);self.assertAlmostEqual(self.report()['ratio'],220/3)
    def test_repeated_close(self):
        first=self.close();self.assertEqual(first,self.s.close(self.a,'s',0,'k',self.executions,[dict(player_id='p',status='present')]))
        self.assertEqual(self.r.db.execute('SELECT count(*) FROM facts').fetchone()[0],1)
    def test_conflicting_key(self):
        self.close();self.executions[0]['minutes']=19
        with self.assertRaises(ConflictError):self.s.close(self.a,'s',0,'k',self.executions,[dict(player_id='p',status='present')])
    def test_window_rollback(self):
        self.items[0]['minutes']=21
        with self.assertRaises(ValidationError):self.approve()
        self.assertEqual(self.r.session('s')['version'],0)
    def test_conflict(self):
        self.approve()
        with self.assertRaises(ConflictError):self.approve()
    def test_access(self):
        with self.assertRaises(AccessError):ReportService(self.r).build(Actor('x',frozenset()),'team','2026-11-01','2026-11-30')
    def test_empty(self):self.assertIsNone(self.report()['ratio'])
    def test_atomic_failure(self):
        self.approve();self.s.start(self.a,'s',1)
        self.r.db.execute("CREATE TRIGGER reject_fact BEFORE INSERT ON facts BEGIN SELECT RAISE(ABORT,'failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.s.close(self.a,'s',0,'k',[dict(item_id=str(i),status='done',minutes=m) for i,m in enumerate((20,30,10))],[dict(player_id='p',status='present')])
        self.assertEqual(self.r.session('s')['state'],'running')
        self.assertEqual(self.r.db.execute('SELECT count(*) FROM requests').fetchone()[0],0)
    def test_missing_attendance(self):
        self.approve();self.s.start(self.a,'s',1)
        with self.assertRaises(ValidationError):self.s.close(self.a,'s',0,'k',[dict(item_id=str(i),status='done',minutes=m) for i,m in enumerate((20,30,10))],[])
    def test_nan(self):
        self.items[0]['minutes']=float('nan')
        with self.assertRaises(ValueError):self.approve()
    def test_snapshot(self):
        self.approve();self.items[0]['minutes']=99
        plan=json.loads(self.r.db.execute('SELECT payload FROM plans').fetchone()[0])
        self.assertEqual(plan['items'][0]['minutes'],20)

if __name__=='__main__':unittest.main()
