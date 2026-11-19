import sqlite3,json,os
from pathlib import Path
from app.errors import DomainError

def connection():
    path=Path(os.environ.get('DB_PATH','data/records.db'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    _migrate(db)
    return db

def _migrate(db):
    cols={r['name'] for r in db.execute('PRAGMA table_info(runs)').fetchall()}
    if 'lifecycle' not in cols:
        db.execute("ALTER TABLE runs ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'active'")
    if 'lineage_id' not in cols:
        db.execute("ALTER TABLE runs ADD COLUMN lineage_id TEXT")
    if 'parent_id' not in cols:
        db.execute("ALTER TABLE runs ADD COLUMN parent_id TEXT")
    if 'correction_no' not in cols:
        db.execute("ALTER TABLE runs ADD COLUMN correction_no INTEGER NOT NULL DEFAULT 0")
    # 旧库回填：每个既有批次自成修订链链头
    db.execute("UPDATE runs SET lineage_id=id WHERE lineage_id IS NULL")
    db.execute('CREATE TABLE IF NOT EXISTS lineage (lineage_id TEXT NOT NULL,parent_id TEXT,successor_id TEXT NOT NULL,correction_no INTEGER NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(lineage_id,successor_id))')
    db.execute('CREATE INDEX IF NOT EXISTS idx_runs_lineage ON runs(lineage_id)')

def _row_to_body(r):
    body=json.loads(r['body'])
    body.update(run_id=r['id'],review_version=r['version'],decision=r['decision'],
        lifecycle=r['lifecycle'],lineage_id=r['lineage_id'],parent_id=r['parent_id'],
        correction_no=r['correction_no'])
    return body

def _get(db,run_id):
    r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','批次不存在',404)
    return _row_to_body(r)

def save(run_id, body):
    with connection() as db:
        db.execute('INSERT OR IGNORE INTO runs(id,body,lifecycle,lineage_id,parent_id,correction_no) VALUES (?,?,?,?,?,?)',
            (run_id,json.dumps(body,ensure_ascii=False),'active',body.get('lineage_id',run_id),body.get('parent_run_id'),int(body.get('correction_no',0))))
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
        saved=_get(db,run_id)
    return saved

def get(run_id):
    with connection() as db:return _get(db,run_id)

def history():
    with connection() as db:
        rows=db.execute("SELECT * FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,rowid DESC LIMIT 100").fetchall()
        return [dict(id=r['id'],version=r['version'],decision=r['decision'],lifecycle=r['lifecycle'],
            lineage_id=r['lineage_id'],parent_id=r['parent_id'],correction_no=r['correction_no']) for r in rows]

def lineage(lineage_id):
    with connection() as db:
        rows=db.execute('SELECT * FROM lineage WHERE lineage_id=? ORDER BY correction_no',(lineage_id,)).fetchall()
        return [dict(r) for r in rows]

def save_correction(parent_id, successor):
    """事务化补正落库：后继插入、父批次转 superseded、链登记、head 切换原子完成。

    任一步失败整体回滚，父批次保持 active，旧版本继续可用。
    同一补正（同 successor_id）重复提交直接返回既有后继，不新增修订。
    """
    successor_id=successor['run_id'];lineage_id=successor['lineage_id']
    with connection() as db:
        parent=db.execute('SELECT * FROM runs WHERE id=?',(parent_id,)).fetchone()
        if not parent:raise DomainError('NOT_FOUND','父批次不存在',404)
        existing=db.execute('SELECT id FROM runs WHERE id=?',(successor_id,)).fetchone()
        if existing:
            db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(successor_id,))
            return _get(db,successor_id)
        if parent['lifecycle']!='active':
            raise DomainError('SUPERSEDED','父批次已被取代，只能对当前有效批次补正',409)
        db.execute('INSERT INTO runs(id,body,version,decision,lifecycle,lineage_id,parent_id,correction_no) VALUES (?,?,?,?,?,?,?,?)',
            (successor_id,json.dumps(successor,ensure_ascii=False),1,'待复核','active',lineage_id,parent_id,int(successor['correction_no'])))
        cur=db.execute("UPDATE runs SET lifecycle='superseded' WHERE id=? AND lifecycle='active'",(parent_id,))
        if cur.rowcount!=1:raise DomainError('SUPERSEDED','父批次状态已变化，请刷新后重试',409)
        db.execute('INSERT INTO lineage(lineage_id,parent_id,successor_id,correction_no,created_at) VALUES (?,?,?,?,?)',
            (lineage_id,parent_id,successor_id,int(successor['correction_no']),successor.get('manifest',{}).get('generated_at','')))
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(successor_id,))
        saved=_get(db,successor_id)
    return saved

def review(run_id, version, decision):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    with connection() as db:
        row=db.execute('SELECT lifecycle FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row:raise DomainError('NOT_FOUND','批次不存在',404)
        if row['lifecycle']!='active':raise DomainError('SUPERSEDED','已被取代的批次不可复核，请在当前有效批次操作',409)
        cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
        if cur.rowcount!=1:
            raise DomainError('STALE','复核版本已更新，请刷新',409)
        saved=_get(db,run_id)
    return saved
