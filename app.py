"""Локальный учебный веб-прототип. Запуск: python app.py."""
from core import *
from dataclasses import asdict
from html import escape
from http.cookies import SimpleCookie
from pathlib import Path
from secrets import token_urlsafe, compare_digest
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server
import os

DB = str(Path(__file__).with_name('hockey.sqlite3'))
TOKENS = {}
PASSWORD = os.environ.get('HOCKEY_PASSWORD','demo123')

@dataclass(frozen=True)
class PlanItem:
    id: str
    exercise_id: str
    minutes: float

@dataclass(frozen=True)
class TrainingPlan:
    items: tuple[PlanItem, ...]
    participants: tuple[str, ...]
    window: float
    def payload(self): return [asdict(x) for x in self.items]

class Sandbox:
    def compare(self, base, candidate, window):
        total=sum(number(x,True) for x in candidate)
        return dict(base=sum(base),total=total,free=window-total,
                    shares=[percent(x,total) for x in candidate])

class TrainingSession:
    """Фасад жизненного цикла: изменения выполняются через сервис с проверками."""
    def __init__(self, service, actor, ident):
        self._service,self._actor,self.id=service,actor,ident
    def approve(self, version, plan):
        return self._service.approve(self._actor,self.id,version,plan.participants,
                                    plan.payload(),plan.window,ROSTER,CATALOG)
    def start(self,version):return self._service.start(self._actor,self.id,version)
    def close(self,revision,key,facts,marks):
        return self._service.close(self._actor,self.id,revision,key,facts,marks)

ROSTER={'p1':'team','p2':'team','p3':'team'}
PLAYERS={'p1':'Игрок 1','p2':'Игрок 2','p3':'Игрок 3'}
CATALOG={str(i):dict(name=name,category=cat,unit='reps') for i,(name,cat) in enumerate([
 ('Разминка','Подготовка'),('Технические упражнения','Техника'),('Игровые упражнения','Игра'),('Завершение','Завершение')])}

def e(x):return escape(str(x),quote=True)
def num(x):return 'Нет данных' if x is None else f'{x:.1f}'
def field(name,value='',kind='text',label=None):
    return f'<label>{e(label or name)}<input type="{kind}" name="{e(name)}" value="{e(value)}" required></label>'
def hidden(name,value):return f'<input type="hidden" name="{e(name)}" value="{e(value)}">'
def select(name,values):
    return f'<select name="{e(name)}">'+''.join(f'<option value="{e(k)}">{e(v)}</option>' for k,v in values)+'</select>'
def form(action,csrf,content):
    return f'<form method="post" action="{action}">'+hidden('csrf',csrf)+content+'</form>'
def page(content):
    return '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Хоккей — журнал тренировок</title>
    <style>body{font:16px system-ui;background:#edf2f7;color:#172b40;margin:0}header{background:#173653;color:white;padding:22px}main{max-width:1050px;margin:25px auto;padding:0 20px}section,form{background:white;padding:22px;margin:16px 0;border-radius:12px}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:10px;border-bottom:1px solid #dde4eb}input,select{padding:9px;margin:6px;max-width:180px;border:1px solid #bdcbd8;border-radius:5px}label{display:inline-block;margin:5px}button,.button{background:#176880;color:white;padding:10px 18px;border:0;border-radius:6px;cursor:pointer}a{color:#126780}nav a{margin-right:22px}small{color:#54687b}.error{background:#fee3df;padding:18px}meter{width:180px}h1{font-size:24px}.cards{display:flex;gap:15px;flex-wrap:wrap}.card{background:#edf6fa;padding:16px}button:focus,input:focus{outline:2px solid #168ba5}</style>
    <header><h1>Учёт тренировок хоккейной команды</h1>Учебный прототип · Python · SQLite</header><main>'''+content+'</main></html>'

class WebApplication:
    def __call__(self,env,start_response):
        status='200 OK';headers=[('Content-Type','text/html; charset=utf-8')]
        repo=SQLiteRepository(DB)
        try:
            path=env.get('PATH_INFO','/');method=env['REQUEST_METHOD']
            cookie=SimpleCookie();cookie.load(env.get('HTTP_COOKIE',''))
            token=cookie.get('hockey');session=TOKENS.get(token.value) if token else None
            data={}
            if method=='POST':
                length=int(env.get('CONTENT_LENGTH') or 0)
                if length>100000:raise ValidationError('Слишком большой запрос')
                data={k:v[-1] for k,v in parse_qs(env['wsgi.input'].read(length).decode(),keep_blank_values=True).items()}
            if path=='/login' and method=='POST':
                if not compare_digest(data.get('password',''),PASSWORD):raise AccessError('Неверный пароль')
                role=data.get('role')
                if role not in ('coach','admin'):raise AccessError('Неизвестная роль')
                t=token_urlsafe(32);TOKENS[t]=(Actor(role,frozenset({'team'}),role),token_urlsafe(24))
                status='303 See Other';headers += [('Location','/'),('Set-Cookie',f'hockey={t}; HttpOnly; SameSite=Strict; Path=/')];content='Вход выполнен'
            elif not session:
                content='<h2>Вход</h2><p>Учебный пароль: demo123. Пользователи и команда демонстрационные.</p>'+form('/login','',select('role',[('coach','Тренер'),('admin','Администратор')])+field('password','','password','Пароль')+'<button>Войти</button>')
            else:
                actor,csrf=session
                if method=='POST' and not compare_digest(data.get('csrf',''),csrf):raise AccessError('Обновите страницу: неверный токен формы')
                service=SessionService(repo)
                if method=='POST':
                    content=self.post(path,data,actor,csrf,service,repo)
                    if content is None:status='303 See Other';headers.append(('Location','/'));content='Сохранено'
                else:content=self.get(path,env,actor,csrf,service,repo)
                content='<nav><a href="/">Занятия</a><a href="/report">Аналитика</a><a href="/sandbox">Песочница</a><a href="/team">Команда</a></nav>'+content
        except (ValidationError,AccessError,ConflictError,ValueError,KeyError,sqlite3.IntegrityError) as ex:
            status='400 Bad Request';content=f'<div class="error">{e(ex)}</div><p><a href="/">Вернуться к занятиям</a></p>'
        except Exception:
            import traceback;traceback.print_exc()
            status='500 Internal Server Error';content='Ошибка сервера. Подробности в терминале.'
        finally:repo.db.close()
        body=page(content).encode();headers.append(('Content-Length',str(len(body))))
        start_response(status,headers);return [body]

    def post(self,path,d,actor,csrf,service,repo):
        if path=='/sandbox':
            SessionService.access(actor,'team')
            values=[float(d[f'm{i}']) for i in range(4)]
            result=Sandbox().compare([10,25,20,5],values,60)
            return self.sandbox(csrf,values,result)
        if path=='/create':
            from uuid import uuid4
            sid=str(uuid4());service.create(actor,sid,'team',d['day'],d['kind']);return None
        sid=d['sid'];obj=TrainingSession(service,actor,sid)
        if path=='/approve':
            parts=tuple(p for p in PLAYERS if d.get(p)=='on')
            plan=TrainingPlan(tuple(PlanItem(str(i),str(i),float(d[f'm{i}'])) for i in range(4)),parts,float(d['window']))
            obj.approve(int(d['version']),plan)
        elif path=='/start':obj.start(int(d['version']))
        elif path=='/close':
            s=repo.session(sid);SessionService.access(actor,s['team'])
            plan=json.loads(repo.db.execute('SELECT payload FROM plans WHERE session=? AND version=?',(sid,s['version'])).fetchone()[0])
            facts=[dict(item_id=x['id'],status=d[f's{x["id"]}'],minutes=float(d[f'f{x["id"]}']) if d[f'f{x["id"]}'].strip() else None,reason=d.get(f'r{x["id"]}','')) for x in plan['items']]
            marks=[dict(player_id=p,status=d[f'a{p}']) for p in plan['participants']]
            obj.close(int(d['revision']),d['key'],facts,marks)
        else:raise ValidationError('Неизвестная операция')
        return None

    def get(self,path,env,actor,csrf,service,repo):
        if path=='/team':return '<h2>Учебный состав</h2><section>'+''.join(f'<p>{e(k)} — {e(v)}</p>' for k,v in PLAYERS.items())+'<p>Справочник демонстрационный. Редактирование состава — следующий этап.</p></section>'
        if actor.role=='admin':return '<section>Роль администратора не разрешает менять результаты тренировок. Доступен просмотр учебного состава команды.</section>'
        if path=='/sandbox':return self.sandbox(csrf,[10,25,20,5])
        if path=='/report':
            q={k:v[-1] for k,v in parse_qs(env.get('QUERY_STRING','')).items()}
            start=q.get('start','2026-01-01');end=q.get('end','2026-12-31')
            r=ReportService(repo).build(actor,'team',start,end,float(q.get('threshold',15)))
            result='<h2>План и факт</h2><form method="get">'+field('start',start,'date','Начало')+field('end',end,'date','Конец')+field('threshold',q.get('threshold',15),'number','Порог отклонения, %')+'<button>Показать</button></form>'
            result+='<section class="cards">'+''.join(f'<div class="card">{label}<h2>{num(r[key])}%</h2></div>' for label,key in [('Факт / план','ratio'),('Полнота','completeness'),('Посещаемость','attendance')])+'</section>'
            result+=f'<p>Закрыто: {r["closed"]}. Незакрыто: {r["pending"]}. Известный факт: {r["known_items"]} из {r["total_items"]}. '+('Данные неполные.' if r['partial'] else '')+'</p>'
            result+='<section><table><tr><th>Упражнение</th><th>План</th><th>Факт</th><th>Отклонение</th><th>Комментарий</th></tr>'
            for x in r['details']:
                result+=f'<tr><td>{e(x["name"])}</td><td>{num(x["plan"])}</td><td>{num(x["fact"])}</td><td>{num(x["relative"])}%</td><td>{"Отклонение сверх порога. " if x["signal"] else ""}{e(x["reason"] or "")}</td></tr>'
            return result+'</table></section>'
        out='<h2>Тренировочные занятия</h2>'+form('/create',csrf,field('day',date.today().isoformat(),'date','Дата')+select('kind',[('ice','Ледовое'),('off_ice','Внеледовое')])+'<button>Создать черновик</button>')
        for s in repo.db.execute('SELECT * FROM sessions WHERE team=? ORDER BY day DESC',('team',)).fetchall():
            common=hidden('sid',s['id'])+hidden('version',s['version'])
            out+=f'<section><h3>{e(s["day"])} · {e(s["kind"])} · {e(s["state"])}</h3><small>Версия плана {s["version"]}, ревизия факта {s["revision"]}</small>'
            if s['state'] in ('draft','planned'):
                values=[10,25,20,5];parts=list(PLAYERS);window=60
                if s['version']:
                    plan=json.loads(repo.db.execute('SELECT payload FROM plans WHERE session=? AND version=?',(s['id'],s['version'])).fetchone()[0]);values=[x['minutes'] for x in plan['items']];parts=plan['participants'];window=plan['window']
                fields=common+field('window',window,'number','Окно занятия, мин')
                fields+=''.join(field(f'm{i}',v,'number',CATALOG[str(i)]['name']) for i,v in enumerate(values))
                fields+=''.join(f'<label><input type="checkbox" name="{p}" {"checked" if p in parts else ""}>{e(name)}</label>' for p,name in PLAYERS.items())
                out+=form('/approve',csrf,fields+'<button>Утвердить план</button>')
                if s['state']=='planned':out+=form('/start',csrf,common+'<button>Начать занятие</button>')
            elif s['state']=='running':
                plan=json.loads(repo.db.execute('SELECT payload FROM plans WHERE session=? AND version=?',(s['id'],s['version'])).fetchone()[0])
                fields=common+hidden('revision',s['revision'])+hidden('key',token_urlsafe(20))
                fields+='<p>Для «Нет данных» оставьте минуты пустыми и укажите причину. Для «Не выполнено» введите 0.</p>'
                for x in plan['items']:
                    ident=x['id'];fields+=f'<p>{e(x["name"])} — план {x["minutes"]} мин '+select('s'+ident,[('done','Выполнено'),('partial','Частично'),('not_done','Не выполнено'),('unknown','Нет данных')])+f'<input aria-label="Фактические минуты" type="number" step="any" name="f{ident}" value="{x["minutes"]}"><input aria-label="Причина" name="r{ident}" placeholder="Причина"></p>'
                for p in plan['participants']:fields+=f'<p>{e(PLAYERS[p])} '+select('a'+p,[('present','Присутствовал'),('absent','Отсутствовал'),('unknown','Нет данных')])+'</p>'
                out+=form('/close',csrf,fields+'<button>Закрыть занятие</button>')
            else:out+='<p>Факт сохранён. <a href="/report">Открыть отчёт</a></p>'
            out+='</section>'
        return out

    def sandbox(self,csrf,values,result=None):
        out='<h2>Песочница плана</h2><p>Учебный исходный план: 10 / 25 / 20 / 5 минут. Изменения не сохраняются.</p>'
        out+=form('/sandbox',csrf,''.join(field(f'm{i}',v,'number',CATALOG[str(i)]['name']) for i,v in enumerate(values))+'<button>Сравнить</button>')
        if result:
            out+=f'<section><h3>Итого: {num(result["total"])} мин</h3><p>{"Остаток" if result["free"]>=0 else "Превышение"}: {num(abs(result["free"]))} мин</p>'
            for i,share in enumerate(result['shares']):out+=f'<p>{e(CATALOG[str(i)]["name"])} <meter min="0" max="100" value="{share}"></meter> {num(share)}%</p>'
            out+='</section>'
        return out

if __name__=='__main__':
    print('Откройте http://127.0.0.1:8000 · пароль demo123 (или HOCKEY_PASSWORD)')
    with make_server('127.0.0.1',8000,WebApplication()) as server:server.serve_forever()
