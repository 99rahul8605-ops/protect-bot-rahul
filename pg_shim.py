"""
pg_shim.py
-----------
Ek lightweight PyMongo-compatible layer jo PostgreSQL (JSONB tables) ko backend
banata hai. Isse existing pymongo-based code (find_one, find, update_one,
insert_one, delete_one, aggregate, create_index) BINA CHANGE kiye chalta hai —
sirf `from pymongo import MongoClient` ko `from pg_shim import PGClient as
MongoClient` se replace karna hota hai.

Design:
- Har Mongo collection => ek Postgres table `<db>_<collection>` (id TEXT PK, data JSONB)
- Ye same schema hai jo migrate.py ne banaya tha, isliye already-migrated data
  turant is shim ke through accessible hai.
- Values bson.json_util se (de)serialize hoti hain, taaki datetime / ObjectId
  jaise Python types transparently round-trip ho jaayein (application code me
  koi change nahi karna padta datetime comparisons ke liye).
"""

import logging
import re
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2 import pool as pg_pool
from bson import json_util, ObjectId

logger = logging.getLogger(__name__)

_MISSING = object()


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name).lower()


# ---------------------------------------------------------------------------
# Filter matching helpers (Python-side; keeps things simple & correct)
# ---------------------------------------------------------------------------

def _get_nested(doc, dotted_key):
    parts = dotted_key.split(".")
    cur = doc
    for p in parts:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
    return cur


def _array_field_matches(doc, dotted_key, cond):
    """Mongo-style dotted match on arrays, e.g. 'batches.id': 'xyz'."""
    if "." not in dotted_key:
        return doc.get(dotted_key) == cond
    arr_field, sub_field = dotted_key.split(".", 1)
    arr = doc.get(arr_field)
    if isinstance(arr, list):
        return any(isinstance(item, dict) and item.get(sub_field) == cond for item in arr)
    return _get_nested(doc, dotted_key) == cond


def _match_ops(val, cond) -> bool:
    for op, opval in cond.items():
        if op == "$gte":
            if val is None or not (val >= opval):
                return False
        elif op == "$lte":
            if val is None or not (val <= opval):
                return False
        elif op == "$gt":
            if val is None or not (val > opval):
                return False
        elif op == "$lt":
            if val is None or not (val < opval):
                return False
        elif op == "$in":
            if val not in opval:
                return False
        elif op == "$ne":
            if val == opval:
                return False
        else:
            return False
    return True


def _matches(doc: dict, filt: dict) -> bool:
    for key, cond in filt.items():
        if key == "$or":
            if not any(_matches(doc, sub) for sub in cond):
                return False
            continue
        if key == "$and":
            if not all(_matches(doc, sub) for sub in cond):
                return False
            continue
        if key == "_id":
            if isinstance(cond, dict):
                if not _match_ops(doc.get("_id"), cond):
                    return False
            else:
                if str(doc.get("_id")) != str(cond):
                    return False
            continue
        if "." in key:
            if not _array_field_matches(doc, key, cond):
                return False
            continue
        val = doc.get(key)
        if isinstance(cond, dict) and any(str(k).startswith("$") for k in cond.keys()):
            if not _match_ops(val, cond):
                return False
        else:
            if val != cond:
                return False
    return True


def _find_positional_index(doc, filt) -> Optional[int]:
    for key, cond in filt.items():
        if key != "_id" and "." in key:
            arr_field, sub_field = key.split(".", 1)
            arr = doc.get(arr_field)
            if isinstance(arr, list):
                for i, item in enumerate(arr):
                    if isinstance(item, dict) and item.get(sub_field) == cond:
                        return i
    return None


def _set_nested(doc, dotted_key, value, positional_index=None):
    parts = dotted_key.split(".")
    cur = doc
    for p in parts[:-1]:
        if p == "$":
            p = positional_index
        if isinstance(cur, list):
            cur = cur[p]
        else:
            nxt = cur.get(p)
            if nxt is None:
                nxt = {}
                cur[p] = nxt
            cur = nxt
    last = parts[-1]
    if last == "$":
        last = positional_index
    cur[last] = value


def _matches_pull_cond(item, cond) -> bool:
    if isinstance(cond, dict):
        return all(item.get(k) == v for k, v in cond.items()) if isinstance(item, dict) else False
    return item == cond


def _apply_update_ops(doc: dict, update: dict, positional_index=None) -> dict:
    doc = dict(doc)
    for op, fields in update.items():
        if op == "$set":
            for k, v in fields.items():
                _set_nested(doc, k, v, positional_index)
        elif op == "$unset":
            for k in fields.keys():
                doc.pop(k, None)
        elif op == "$inc":
            for k, v in fields.items():
                cur = _get_nested(doc, k) or 0
                _set_nested(doc, k, cur + v, positional_index)
        elif op == "$push":
            for k, v in fields.items():
                arr = doc.get(k)
                if not isinstance(arr, list):
                    arr = []
                arr.append(v)
                doc[k] = arr
        elif op == "$pull":
            for k, cond in fields.items():
                arr = doc.get(k)
                if isinstance(arr, list):
                    doc[k] = [item for item in arr if not _matches_pull_cond(item, cond)]
        elif op == "$setOnInsert":
            for k, v in fields.items():
                if k not in doc:
                    doc[k] = v
        # unknown ops are ignored silently (none used in this codebase)
    return doc


# ---------------------------------------------------------------------------
# Cursor (supports .sort()/.limit() chaining, like pymongo)
# ---------------------------------------------------------------------------

class _Cursor:
    def __init__(self, docs: List[dict]):
        self._docs = list(docs)

    def sort(self, key, direction: int = 1):
        if isinstance(key, list):
            keys = key
        else:
            keys = [(key, direction)]
        for k, d in reversed(keys):
            self._docs.sort(key=lambda x: (x.get(k) is None, x.get(k)), reverse=(d == -1))
        return self

    def limit(self, n: int):
        self._docs = self._docs[:n]
        return self

    def __iter__(self):
        return iter(self._docs)

    def __len__(self):
        return len(self._docs)

    def __getitem__(self, idx):
        return self._docs[idx]


# ---------------------------------------------------------------------------
# Connection pool (shared across all collections)
# ---------------------------------------------------------------------------

class _Pool:
    _pool = None

    @classmethod
    def init(cls, dsn: str):
        if cls._pool is None:
            cls._pool = pg_pool.ThreadedConnectionPool(1, 10, dsn=dsn)

    @classmethod
    def getconn(cls):
        return cls._pool.getconn()

    @classmethod
    def putconn(cls, conn):
        cls._pool.putconn(conn)


class _ConnCtx:
    def __enter__(self):
        self.conn = _Pool.getconn()
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            try:
                self.conn.rollback()
            except Exception:
                pass
        _Pool.putconn(self.conn)


# ---------------------------------------------------------------------------
# Collection (pymongo-compatible subset)
# ---------------------------------------------------------------------------

class PGCollection:
    def __init__(self, db_name: str, coll_name: str):
        self.db_name = db_name
        self.coll_name = coll_name
        self.table = _sanitize(f"{db_name}_{coll_name}")
        self._ensure_table()

    def _ensure_table(self):
        with _ConnCtx() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f'CREATE TABLE IF NOT EXISTS "{self.table}" ('
                    f'id TEXT PRIMARY KEY, data JSONB NOT NULL);'
                )
            conn.commit()

    # -- internal helpers --------------------------------------------------

    def _fetch_all(self) -> List[dict]:
        with _ConnCtx() as conn:
            with conn.cursor() as cur:
                cur.execute(f'SELECT id, data::text FROM "{self.table}"')
                rows = cur.fetchall()
        docs = []
        for rid, data_text in rows:
            doc = json_util.loads(data_text)
            doc["_id"] = rid
            docs.append(doc)
        return docs

    def _fetch_by_field(self, field: str, value) -> List[dict]:
        with _ConnCtx() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f'SELECT id, data::text FROM "{self.table}" WHERE data->>%s = %s LIMIT 200',
                    (field, str(value)),
                )
                rows = cur.fetchall()
        docs = []
        for rid, data_text in rows:
            doc = json_util.loads(data_text)
            doc["_id"] = rid
            docs.append(doc)
        return docs

    def _apply_projection(self, doc: dict, projection: Optional[dict]) -> dict:
        if not projection:
            return doc
        include_id = projection.get("_id", 1) != 0
        fields = [k for k, v in projection.items() if v == 1 and k != "_id"]
        out = {k: doc.get(k) for k in fields}
        if include_id:
            out["_id"] = doc.get("_id")
        return out

    def _upsert_doc(self, doc: dict):
        doc = dict(doc)
        doc_id = str(doc.get("_id") or ObjectId())
        doc["_id"] = doc_id
        data_json = json_util.dumps(doc)
        with _ConnCtx() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f'INSERT INTO "{self.table}" (id, data) VALUES (%s, %s::jsonb) '
                    f'ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data',
                    (doc_id, data_json),
                )
            conn.commit()
        return doc_id

    # -- public pymongo-like API --------------------------------------------

    def find_one(self, filter: Optional[dict] = None, projection: Optional[dict] = None):
        filt = dict(filter or {})

        if "_id" in filt and not isinstance(filt["_id"], dict):
            target_id = str(filt["_id"])
            with _ConnCtx() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f'SELECT id, data::text FROM "{self.table}" WHERE id = %s', (target_id,)
                    )
                    row = cur.fetchone()
            if not row:
                return None
            doc = json_util.loads(row[1])
            doc["_id"] = row[0]
            rest = {k: v for k, v in filt.items() if k != "_id"}
            if rest and not _matches(doc, rest):
                return None
            return self._apply_projection(doc, projection)

        simple_keys = [
            k for k, v in filt.items()
            if k not in ("$or", "$and") and "." not in k and not isinstance(v, dict)
        ]
        if simple_keys:
            field = simple_keys[0]
            candidates = self._fetch_by_field(field, filt[field])
            for doc in candidates:
                if _matches(doc, filt):
                    return self._apply_projection(doc, projection)
            return None

        for doc in self._fetch_all():
            if _matches(doc, filt):
                return self._apply_projection(doc, projection)
        return None

    def find(self, filter: Optional[dict] = None, projection: Optional[dict] = None,
              sort=None, limit: Optional[int] = None) -> _Cursor:
        filt = filter or {}
        docs = [d for d in self._fetch_all() if _matches(d, filt)]
        cur = _Cursor(docs)
        if sort:
            cur.sort(sort)
        if limit:
            cur.limit(limit)
        if projection:
            cur._docs = [self._apply_projection(d, projection) for d in cur._docs]
        return cur

    def count_documents(self, filter: Optional[dict] = None) -> int:
        filt = filter or {}
        if not filt:
            with _ConnCtx() as conn:
                with conn.cursor() as cur:
                    cur.execute(f'SELECT COUNT(*) FROM "{self.table}"')
                    return cur.fetchone()[0]
        return sum(1 for d in self._fetch_all() if _matches(d, filt))

    def insert_one(self, doc: dict):
        doc = dict(doc)
        if not doc.get("_id"):
            doc["_id"] = str(ObjectId())
        doc_id = self._upsert_doc(doc)
        return SimpleNamespace(inserted_id=doc_id)

    def update_one(self, filter: dict, update: dict, upsert: bool = False):
        filt = filter or {}
        doc = self.find_one(filt)
        if doc is None:
            if upsert:
                new_doc = {
                    k: v for k, v in filt.items()
                    if not str(k).startswith("$") and "." not in k and not isinstance(v, dict)
                }
                new_doc = _apply_update_ops(new_doc, update)
                if not new_doc.get("_id"):
                    new_doc["_id"] = str(ObjectId())
                doc_id = self._upsert_doc(new_doc)
                return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=doc_id)
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)

        positional_index = _find_positional_index(doc, filt)
        new_doc = _apply_update_ops(doc, update, positional_index)
        self._upsert_doc(new_doc)
        return SimpleNamespace(matched_count=1, modified_count=1, upserted_id=None)

    def delete_one(self, filter: dict):
        doc = self.find_one(filter)
        if not doc:
            return SimpleNamespace(deleted_count=0)
        with _ConnCtx() as conn:
            with conn.cursor() as cur:
                cur.execute(f'DELETE FROM "{self.table}" WHERE id = %s', (str(doc["_id"]),))
            conn.commit()
        return SimpleNamespace(deleted_count=1)

    def create_index(self, keys, unique: bool = False):
        try:
            if isinstance(keys, str):
                field = keys
            elif isinstance(keys, list) and keys:
                field = keys[0][0]
            else:
                return
            idx_name = _sanitize(f"{self.table}_{field}_idx")
            unique_sql = "UNIQUE " if unique else ""
            with _ConnCtx() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f'CREATE {unique_sql}INDEX IF NOT EXISTS "{idx_name}" '
                        f'ON "{self.table}" ((data->>%s))',
                        (field,),
                    )
                conn.commit()
        except Exception as e:
            logger.warning(f"create_index skipped for {self.table}: {e}")

    def aggregate(self, pipeline: List[dict]):
        docs = self._fetch_all()
        for stage in pipeline:
            if "$match" in stage:
                docs = [d for d in docs if _matches(d, stage["$match"])]
            elif "$group" in stage:
                group = stage["$group"]
                id_expr = group["_id"]
                buckets: Dict[Any, list] = {}
                for d in docs:
                    if id_expr is None:
                        key = None
                    elif isinstance(id_expr, str) and id_expr.startswith("$"):
                        key = _get_nested(d, id_expr[1:])
                    else:
                        key = id_expr
                    buckets.setdefault(key, []).append(d)
                results = []
                for key, items in buckets.items():
                    row = {"_id": key}
                    for out_field, agg_expr in group.items():
                        if out_field == "_id":
                            continue
                        if isinstance(agg_expr, dict) and "$sum" in agg_expr:
                            sum_expr = agg_expr["$sum"]
                            if sum_expr == 1:
                                row[out_field] = len(items)
                            elif isinstance(sum_expr, str) and sum_expr.startswith("$"):
                                fld = sum_expr[1:]
                                row[out_field] = sum((_get_nested(it, fld) or 0) for it in items)
                            else:
                                row[out_field] = len(items)
                    results.append(row)
                docs = results
            elif "$sort" in stage:
                for k, direction in reversed(list(stage["$sort"].items())):
                    docs.sort(key=lambda x: (x.get(k) is None, x.get(k)), reverse=(direction == -1))
            elif "$limit" in stage:
                docs = docs[: stage["$limit"]]
        return docs


# ---------------------------------------------------------------------------
# Database / Client wrappers (mimic pymongo's client[db][collection] access)
# ---------------------------------------------------------------------------

class PGDatabase:
    def __init__(self, db_name: str):
        self.db_name = db_name
        self._collections: Dict[str, PGCollection] = {}

    def __getitem__(self, coll_name: str) -> PGCollection:
        if coll_name not in self._collections:
            self._collections[coll_name] = PGCollection(self.db_name, coll_name)
        return self._collections[coll_name]


class _AdminShim:
    def command(self, cmd, *args, **kwargs):
        with _ConnCtx() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return {"ok": 1.0}


class PGClient:
    """Drop-in replacement for pymongo.MongoClient, backed by PostgreSQL."""

    def __init__(self, dsn: str):
        _Pool.init(dsn)
        self.admin = _AdminShim()
        self._dbs: Dict[str, PGDatabase] = {}

    def __getitem__(self, db_name: str) -> PGDatabase:
        if db_name not in self._dbs:
            self._dbs[db_name] = PGDatabase(db_name)
        return self._dbs[db_name]
