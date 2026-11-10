"""Алгоритмы раздела 6. Python 3.10+, только стандартная библиотека.
Actor создаётся доверенным серверным слоем после авторизации, не из тела HTTP-запроса.
"""
import json
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone

class ValidationError(ValueError): pass
class AccessError(PermissionError): pass
class ConflictError(RuntimeError): pass

@dataclass(frozen=True)
class Actor:
    id: str
    teams: frozenset[str]
    role: str = 'coach'


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)

def number(value, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError('Ожидается число')
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValidationError('Недопустимое числовое значение')
    return value

def unique(rows, key):
    result = {}
    for row in rows:
        ident = row.get(key)
        if not isinstance(ident, str) or not ident.strip() or ident in result:
            raise ValidationError(f'Пустой или повторяющийся {key}')
        result[ident] = row
    return result

def percent(n, d):
    return 100 * n / d if d else None

class SQLiteRepository:
    """Одна связь на запрос/поток. JSON-снимки не меняются после вставки."""
    def __init__(self, path='hockey.sqlite3'):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS sessions(
          id TEXT PRIMARY KEY, team TEXT NOT NULL, day TEXT NOT NULL,
          kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'draft',
          version INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS plans(
          session TEXT REFERENCES sessions(id), version INTEGER, payload TEXT NOT NULL,
          PRIMARY KEY(session,version));
        CREATE TABLE IF NOT EXISTS facts(
          session TEXT REFERENCES sessions(id), revision INTEGER, payload TEXT NOT NULL,
          PRIMARY KEY(session,revision));
        CREATE TABLE IF NOT EXISTS requests(
          actor TEXT, session TEXT REFERENCES sessions(id), key TEXT,
          payload TEXT NOT NULL, response TEXT NOT NULL,
          PRIMARY KEY(actor,session,key));
        CREATE TABLE IF NOT EXISTS audit(
          id INTEGER PRIMARY KEY, session TEXT, actor TEXT, moment TEXT,
          action TEXT, details TEXT);
        CREATE INDEX IF NOT EXISTS selection ON sessions(team,day);
        ''')

    @contextmanager
    def transaction(self, write=True):
        self.db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def session(self, ident):
        row = self.db.execute('SELECT * FROM sessions WHERE id=?', (ident,)).fetchone()
        if row is None: raise ValidationError('Занятие не найдено')
        return row

    def audit(self, actor, sid, action, details):
        self.db.execute('INSERT INTO audit(session,actor,moment,action,details) VALUES(?,?,?,?,?)',
            (sid, actor.id, datetime.now(timezone.utc).isoformat(), action, encode(details)))

from typing import Protocol

class SessionRepository(Protocol):
    db: sqlite3.Connection
    def transaction(self, write: bool = True): ...
    def session(self, ident: str): ...
    def audit(self, actor: Actor, sid: str, action: str, details: dict): ...

class SessionService:
    def __init__(self, repository: SessionRepository):
        self.repo = repository

    @staticmethod
    def access(actor, team):
        if actor.role != 'coach' or team not in actor.teams:
            raise AccessError('Нет прав тренера для этой команды')

    def create(self, actor, sid, team, day, kind='ice'):
        self.access(actor, team)
        date.fromisoformat(day)
        if kind not in ('ice', 'off_ice'): raise ValidationError('Неизвестный вид занятия')
        if not isinstance(sid, str) or not sid.strip(): raise ValidationError('Пустой ID')
        with self.repo.transaction():
            self.repo.db.execute('INSERT INTO sessions(id,team,day,kind) VALUES(?,?,?,?)',
                                 (sid, team, day, kind))
            self.repo.audit(actor, sid, 'create', {'reason': 'Создание черновика'})

    def approve(self, actor, sid, expected_version, participants, items, window,
                roster, catalog):
        """roster и catalog загружаются сервером из доверенного справочника.
        roster: {player_id: team_id}; catalog: {exercise_id: {name,category,unit}}.
        item: {id,exercise_id,minutes,volume?,unit?}. Порядок задаётся списком.
        """
        # Копирование защищает от последующего изменения переданных списков.
        items = json.loads(encode(items)); participants = list(participants)
        with self.repo.transaction():
            s = self.repo.session(sid); self.access(actor, s['team'])
            if s['state'] not in ('draft', 'planned'): raise ValidationError('План уже зафиксирован')
            if s['version'] != expected_version: raise ConflictError(f"Актуальная версия: {s['version']}")
            if not participants or not items: raise ValidationError('Нужны участники и упражнения')
            unique([{'id': x} for x in participants], 'id'); unique(items, 'id')
            if any(roster.get(x) != s['team'] for x in participants):
                raise ValidationError('Участник не принадлежит команде')
            number(window, True)
            total = 0
            for position, item in enumerate(items, 1):
                exercise = catalog.get(item.get('exercise_id'))
                if exercise is None: raise ValidationError('Нет упражнения в каталоге')
                total += number(item.get('minutes'), True)
                if item.get('volume') is not None:
                    number(item['volume'])
                    if not exercise.get('unit') or item.get('unit') != exercise['unit']:
                        raise ValidationError('Несопоставимая единица объёма')
                elif item.get('unit') not in (None, exercise.get('unit')):
                    raise ValidationError('Недопустимая единица')
                item.update(name=exercise['name'], category=exercise['category'], position=position)
            if total > window: raise ValidationError('Превышено временное окно')
            version = s['version'] + 1
            plan = dict(participants=participants, items=items, window=window)
            self.repo.db.execute('INSERT INTO plans VALUES(?,?,?)', (sid, version, encode(plan)))
            self.repo.db.execute("UPDATE sessions SET version=?,state='planned' WHERE id=?", (version,sid))
            self.repo.audit(actor,sid,'approve',{'version':version,'previous':s['version'], 'reason':'Утверждение плана'})
            return {'version':version, 'free_minutes':window-total}

    def start(self, actor, sid, expected_version):
        with self.repo.transaction():
            s = self.repo.session(sid); self.access(actor,s['team'])
            if s['version'] != expected_version: raise ConflictError('Версия плана изменилась')
            if s['state'] != 'planned': raise ValidationError('Занятие не запланировано')
            self.repo.db.execute("UPDATE sessions SET state='running' WHERE id=?",(sid,))
            self.repo.audit(actor,sid,'start',{'base_version':s['version'],'reason':'Начало занятия'})

    def close(self, actor, sid, expected_revision, key, executions, attendance, extra=None):
        if not isinstance(key,str) or not key.strip(): raise ValidationError('Нужен ключ запроса')
        body = json.loads(encode(dict(revision=expected_revision,executions=executions,
                                     attendance=attendance,extra=extra or [])))
        payload = encode(body)
        with self.repo.transaction():
            s = self.repo.session(sid); self.access(actor,s['team'])
            old = self.repo.db.execute('SELECT * FROM requests WHERE actor=? AND session=? AND key=?',
                                      (actor.id,sid,key)).fetchone()
            # Повтор проверяется ДО состояния, поскольку первое закрытие уже сменило его.
            if old:
                if old['payload'] != payload: raise ConflictError('Ключ использован с другим содержимым')
                return json.loads(old['response'])
            if s['revision'] != expected_revision: raise ConflictError('Ревизия изменилась')
            if s['state'] != 'running': raise ValidationError('Занятие не проводится')
            plan = json.loads(self.repo.db.execute('SELECT payload FROM plans WHERE session=? AND version=?',
                                                  (sid,s['version'])).fetchone()[0])
            planned = unique(plan['items'],'id'); actual = unique(body['executions'],'item_id')
            if planned.keys() != actual.keys(): raise ValidationError('Не совпадает набор элементов плана и факта')
            for ident, row in actual.items():
                status = row.get('status'); minutes = row.get('minutes')
                if status == 'unknown':
                    if minutes is not None or row.get('volume') is not None or not str(row.get('reason') or '').strip():
                        raise ValidationError('Нет данных: нужны null и причина')
                elif status in ('done','partial','not_done'):
                    number(minutes)
                    if status == 'not_done' and minutes != 0: raise ValidationError('Невыполнение означает 0')
                    if status in ('done','partial') and minutes == 0: raise ValidationError('Выполнение требует времени > 0')
                    if row.get('volume') is not None:
                        number(row['volume'])
                        if not planned[ident].get('unit') or row.get('unit') != planned[ident]['unit']:
                            raise ValidationError('Несопоставимая единица факта')
                        if status == 'not_done' and row['volume'] != 0: raise ValidationError('Объём невыполненного равен 0')
                else: raise ValidationError('Неизвестный статус выполнения')
            marks = unique(body['attendance'],'player_id')
            if set(marks) != set(plan['participants']): raise ValidationError('Неполная посещаемость')
            if any(x.get('status') not in ('present','absent','unknown') for x in marks.values()):
                raise ValidationError('Неизвестный статус посещаемости')
            unique(body['extra'],'id')
            for row in body['extra']:
                if row.get('session_id') != sid: raise ValidationError('Внеплановая запись другого занятия')
                number(row.get('minutes'))
            rev = s['revision']+1
            self.repo.db.execute('INSERT INTO facts VALUES(?,?,?)',(sid,rev,payload))
            self.repo.db.execute("UPDATE sessions SET revision=?,state='closed' WHERE id=?",(rev,sid))
            result = {'state':'closed','revision':rev}
            self.repo.audit(actor,sid,'close',{'revision':rev,'previous':s['revision'],'reason':'Закрытие занятия'})
            self.repo.db.execute('INSERT INTO requests VALUES(?,?,?,?,?)',(actor.id,sid,key,payload,encode(result)))
            return result

class ReportService:
    RULES = '1.0'
    def __init__(self, repository: SessionRepository): self.repo = repository

    def build(self, actor, team, start, end, threshold=15, kind=None):
        SessionService.access(actor,team)
        if date.fromisoformat(start) > date.fromisoformat(end): raise ValidationError('Неверный период')
        number(threshold)
        if kind not in (None,'ice','off_ice'): raise ValidationError('Неизвестный вид')
        # day — календарная дата команды. Один SELECT даёт согласованную выборку.
        with self.repo.transaction(False):
            rows = self.repo.db.execute('''SELECT s.*,p.payload plan,f.payload fact FROM sessions s
                LEFT JOIN plans p ON p.session=s.id AND p.version=s.version
                LEFT JOIN facts f ON f.session=s.id AND f.revision=s.revision
                WHERE s.team=? AND s.day BETWEEN ? AND ? AND (? IS NULL OR s.kind=?)''',
                (team,start,end,kind,kind)).fetchall()
        total=known=done=people=known_people=present=0
        all_plan=known_plan=fact_minutes=extra_minutes=0
        closed=cancelled=pending=0; detail=[]; versions=[]
        for s in rows:
            if s['state']=='cancelled': cancelled+=1; continue
            if s['state']!='closed': pending+=1; continue
            closed+=1; plan=json.loads(s['plan']); fact=json.loads(s['fact'])
            versions.append({'session':s['id'],'plan':s['version'],'fact':s['revision']})
            actual=unique(fact['executions'],'item_id')
            for item in plan['items']:
                row=actual[item['id']]; p=item['minutes']; f=row['minutes']
                total+=1; all_plan+=p
                delta=relative=None
                if f is not None:
                    known+=1; known_plan+=p; fact_minutes+=f
                    delta=f-p; relative=percent(delta,p)
                done+=row['status']=='done'
                detail.append(dict(session=s['id'],item=item['id'],name=item['name'],plan=p,
                    fact=f,delta=delta,relative=relative,status=row['status'],reason=row.get('reason'),
                    signal=relative is not None and abs(relative)>threshold))
            for mark in fact['attendance']:
                people+=1; known_people+=mark['status']!='unknown'; present+=mark['status']=='present'
            extra_minutes+=sum(x['minutes'] for x in fact['extra'])
        return dict(rules=self.RULES,generated_at=datetime.now(timezone.utc).isoformat(),
            filters=dict(team=team,start=start,end=end,kind=kind,threshold=threshold),
            closed=closed,cancelled=cancelled,pending=pending,
            ratio=percent(fact_minutes,known_plan),completeness=percent(known,total),
            time_completeness=percent(known_plan,all_plan),exercise_completion=percent(done,known),
            attendance=percent(present,known_people),attendance_known=known_people,attendance_total=people,
            known_items=known,total_items=total,missing_items=total-known,
            known_plan_minutes=known_plan,fact_minutes=fact_minutes,extra_minutes=extra_minutes,
            partial=known<total or known_people<people,details=detail,versions=versions)
