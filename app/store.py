import sqlite3, json, hashlib
from datetime import datetime, timezone
from app.config import DATA_DIR, RULE_VERSION
from app.errors import BusinessError

def connect():
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(DATA_DIR/"runs.sqlite", timeout=15)
    db.row_factory=sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, decision TEXT NOT NULL DEFAULT '待复核', created TEXT NOT NULL)")
    # 空白依据切换的修正记录。以序列摘要+样品+分析项+来源行+basis 唯一：
    # 同一序列重跑不会重复追加；规则调整后 basis 变化才新增一条。
    # 旧库若为早期无 id 列的版本，直接重建（修正记录可由结果重算，非原始数据）。
    columns={row["name"] for row in db.execute("PRAGMA table_info(amendments)")}
    if columns and "id" not in columns:
        db.execute("DROP TABLE amendments")
    db.execute("CREATE TABLE IF NOT EXISTS amendments (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, digest TEXT NOT NULL, sample_id TEXT NOT NULL, analyte TEXT NOT NULL, batch TEXT NOT NULL, line INTEGER NOT NULL, basis TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS amendments_identity ON amendments(digest,sample_id,analyte,batch,line,basis)")
    return db

def _append_amendments(db, ident, digest, result):
    created=datetime.now(timezone.utc).isoformat()
    for row in result.get("corrections",[]):
        if row.get("status") not in {"APPLIED","KEPT_ORIGINAL"}:
            continue
        identity="|".join([digest,row["sample_id"],row["analyte"],row["batch"],str(row["line"]),row["basis"]])
        record_id=hashlib.sha256(identity.encode()).hexdigest()[:16]
        db.execute("INSERT OR IGNORE INTO amendments(id,run_id,digest,sample_id,analyte,batch,line,basis,status,payload,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(
            record_id,ident,digest,row["sample_id"],row["analyte"],row["batch"],row["line"],row["basis"],row["status"],
            json.dumps(row,ensure_ascii=False),created))

def amendments(ident):
    with connect() as db:
        rows=db.execute("SELECT * FROM amendments WHERE run_id=? ORDER BY created,line,basis",(ident,)).fetchall()
    out=[]
    for row in rows:
        item=dict(row);item["payload"]=json.loads(item["payload"]);out.append(item)
    return out

def save(text, result):
    digest=hashlib.sha256((RULE_VERSION+"\n"+text).encode()).hexdigest()
    ident=digest[:16]
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO runs(id,digest,result,created) VALUES(?,?,?,?)",(ident,digest,json.dumps(dict(result,rule_version=RULE_VERSION),ensure_ascii=False),datetime.now(timezone.utc).isoformat()))
        _append_amendments(db,ident,digest,result)
    return get(ident)

def get(ident):
    with connect() as db:
        row=db.execute("SELECT * FROM runs WHERE id=?",(ident,)).fetchone()
    if row is None: raise BusinessError("NOT_FOUND","批次不存在",404)
    item=dict(row);item["result"]=json.loads(item["result"]);item["rule_version"]=item["result"].get("rule_version","2026.11.18.1")
    item["amendments"]=amendments(ident)
    return item

def history():
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT id,revision,decision,created FROM runs ORDER BY created DESC LIMIT 40")]

def review(ident,revision,decision):
    if not isinstance(decision,str) or decision not in {"通过","退回"}: raise BusinessError("DECISION","复核决定无效")
    current=get(ident)
    if decision=="通过" and current["result"].get("blocked"):
        raise BusinessError("BLOCKED","存在阻断项，请先修正数据",409)
    with connect() as db:
        changed=db.execute("UPDATE runs SET revision=revision+1,decision=? WHERE id=? AND revision=?",(decision,ident,revision)).rowcount
        if not changed: raise BusinessError("STALE","版本已更新，请刷新",409)
    return get(ident)
