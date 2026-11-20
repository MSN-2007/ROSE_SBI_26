"""
memory/graph_memory.py  —  ROSE Knowledge Graph Engine
=======================================================
Replaces the flat 6-bucket JSON store with a SQLite-backed knowledge graph.

Schema
------
  nodes  (id TEXT PK, type TEXT, name TEXT, properties JSON, updated_at TEXT)
  edges  (from_id TEXT, to_id TEXT, predicate TEXT, weight REAL,
          metadata JSON, created_at TEXT)

No new dependencies — uses stdlib sqlite3 only.

Public API (mirror of memory_manager.py where possible for drop-in compat)
---------------------------------------------------------------------------
  add_node(name, type, **props)       -> node_id
  add_edge(from_name, to_name, predicate, weight=1.0, **meta)
  get_node(name_or_id)                -> dict | None
  update_node(name_or_id, **props)
  delete_node(name_or_id)
  search(query, limit=10)             -> list[dict]
  neighbors(name_or_id, predicate=None, depth=1)  -> list[dict]
  format_for_prompt(budget=900)       -> str
  import_flat_memory(flat: dict)      (seed from old long_term.json)
  remember(key, value, category)      (compat shim)
  forget(key, category)               (compat shim)
  search_memory(query, limit)         (compat shim)
  all_entries_for_ui()                (compat shim)
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any


# ── Paths ──────────────────────────────────────────────────────────────────────

def _base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


DB_PATH = _base() / "memory" / "knowledge_graph.db"
_lock   = Lock()


# ── Schema ─────────────────────────────────────────────────────────────────────

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL DEFAULT 'fact',
    name        TEXT NOT NULL,
    properties  TEXT NOT NULL DEFAULT '{}',
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    from_id    TEXT NOT NULL,
    to_id      TEXT NOT NULL,
    predicate  TEXT NOT NULL,
    weight     REAL NOT NULL DEFAULT 1.0,
    metadata   TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY (from_id, to_id, predicate)
);

CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to   ON edges(to_id);
CREATE INDEX IF NOT EXISTS idx_edges_pred ON edges(predicate);

-- Full-text search over node names + properties
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    id UNINDEXED, name, properties, content='nodes', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts(rowid, id, name, properties)
    VALUES (new.rowid, new.id, new.name, new.properties);
END;
CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, id, name, properties)
    VALUES ('delete', old.rowid, old.id, old.name, old.properties);
    INSERT INTO nodes_fts(rowid, id, name, properties)
    VALUES (new.rowid, new.id, new.name, new.properties);
END;
CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, id, name, properties)
    VALUES ('delete', old.rowid, old.id, old.name, old.properties);
END;
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _init_db() -> None:
    with _lock:
        con = _connect()
        try:
            con.executescript(_DDL)
        finally:
            con.close()


_init_db()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _node_id(name: str, type_: str = "") -> str:
    """Deterministic ID: stable across restarts so edges survive re-imports."""
    raw = f"{type_.lower()}::{name.lower().strip()}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _loads(s: str) -> dict:
    try:
        return json.loads(s or "{}")
    except Exception:
        return {}


def _dumps(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False)


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["properties"] = _loads(d.get("properties", "{}"))
    return d


# ── Node CRUD ──────────────────────────────────────────────────────────────────

def add_node(name: str, type_: str = "fact", **props) -> str:
    """
    Insert or update a node. Returns its stable id.

    Common types: person, project, device, place, skill, event, fact, preference
    """
    nid  = _node_id(name, type_)
    now  = _now()
    with _lock:
        con = _connect()
        try:
            row = con.execute("SELECT properties FROM nodes WHERE id=?", (nid,)).fetchone()
            if row:
                existing = _loads(row[0])
                existing.update(props)
                con.execute(
                    "UPDATE nodes SET name=?, type=?, properties=?, updated_at=? WHERE id=?",
                    (name, type_, _dumps(existing), now, nid),
                )
            else:
                con.execute(
                    "INSERT INTO nodes(id, type, name, properties, updated_at) VALUES(?,?,?,?,?)",
                    (nid, type_, name, _dumps(props), now),
                )
            con.commit()
        finally:
            con.close()
    return nid


def get_node(name_or_id: str) -> dict | None:
    """Fetch a node by its id or exact name."""
    with _lock:
        con = _connect()
        try:
            row = con.execute("SELECT * FROM nodes WHERE id=?", (name_or_id,)).fetchone()
            if not row:
                row = con.execute(
                    "SELECT * FROM nodes WHERE name=? COLLATE NOCASE LIMIT 1",
                    (name_or_id,),
                ).fetchone()
            return _row_to_dict(row)
        finally:
            con.close()


def update_node(name_or_id: str, **props) -> bool:
    node = get_node(name_or_id)
    if not node:
        return False
    existing = node["properties"]
    existing.update(props)
    with _lock:
        con = _connect()
        try:
            con.execute(
                "UPDATE nodes SET properties=?, updated_at=? WHERE id=?",
                (_dumps(existing), _now(), node["id"]),
            )
            con.commit()
        finally:
            con.close()
    return True


def delete_node(name_or_id: str) -> bool:
    node = get_node(name_or_id)
    if not node:
        return False
    nid = node["id"]
    with _lock:
        con = _connect()
        try:
            con.execute("DELETE FROM edges WHERE from_id=? OR to_id=?", (nid, nid))
            con.execute("DELETE FROM nodes WHERE id=?", (nid,))
            con.commit()
        finally:
            con.close()
    return True


# ── Edge CRUD ──────────────────────────────────────────────────────────────────

def add_edge(
    from_name: str, to_name: str, predicate: str,
    from_type: str = "fact", to_type: str = "fact",
    weight: float = 1.0, **meta
) -> None:
    """
    Create a directed edge from_name -[predicate]-> to_name.
    Nodes are created automatically if they don't exist.
    """
    fid = add_node(from_name, from_type)
    tid = add_node(to_name,   to_type)
    now = _now()
    with _lock:
        con = _connect()
        try:
            con.execute(
                """INSERT OR REPLACE INTO edges(from_id, to_id, predicate, weight, metadata, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (fid, tid, predicate, weight, _dumps(meta), now),
            )
            con.commit()
        finally:
            con.close()


def neighbors(
    name_or_id: str,
    predicate: str | None = None,
    depth: int = 1,
    direction: str = "both",   # "out", "in", "both"
) -> list[dict]:
    """
    Return nodes reachable from name_or_id within `depth` hops.
    Each result dict has keys: id, name, type, properties, predicate, weight, direction
    """
    node = get_node(name_or_id)
    if not node:
        return []

    nid     = node["id"]
    visited = {nid}
    frontier = [nid]
    results  = []

    with _lock:
        con = _connect()
        try:
            for _ in range(depth):
                next_frontier = []
                for fid in frontier:
                    rows = []
                    if direction in ("out", "both"):
                        q = "SELECT e.*, n.* FROM edges e JOIN nodes n ON n.id=e.to_id WHERE e.from_id=?"
                        args: list = [fid]
                        if predicate:
                            q += " AND e.predicate=?"
                            args.append(predicate)
                        rows += con.execute(q, args).fetchall()
                    if direction in ("in", "both"):
                        q = "SELECT e.*, n.* FROM edges e JOIN nodes n ON n.id=e.from_id WHERE e.to_id=?"
                        args = [fid]
                        if predicate:
                            q += " AND e.predicate=?"
                            args.append(predicate)
                        rows += con.execute(q, args).fetchall()
                    for row in rows:
                        d = dict(row)
                        tid = d["to_id"] if direction != "in" else d["from_id"]
                        if tid not in visited:
                            visited.add(tid)
                            next_frontier.append(tid)
                            results.append({
                                "id":        d.get("id", tid),
                                "name":      d.get("name", ""),
                                "type":      d.get("type", ""),
                                "properties": _loads(d.get("properties", "{}")),
                                "predicate": d.get("predicate", ""),
                                "weight":    d.get("weight", 1.0),
                            })
                frontier = next_frontier
                if not frontier:
                    break
        finally:
            con.close()

    results.sort(key=lambda r: -r["weight"])
    return results


# ── Search ─────────────────────────────────────────────────────────────────────

def search(query: str, limit: int = 10) -> list[dict]:
    """
    FTS search over node names + properties.
    Falls back to LIKE if FTS returns nothing.
    """
    if not query or not query.strip():
        return _all_nodes(limit)

    with _lock:
        con = _connect()
        try:
            safe = re.sub(r"[^\w\s]", " ", query).strip()
            fts_q = " OR ".join(safe.split())
            rows = con.execute(
                """SELECT n.* FROM nodes_fts f
                   JOIN nodes n ON n.id = f.id
                   WHERE nodes_fts MATCH ?
                   ORDER BY rank LIMIT ?""",
                (fts_q, limit),
            ).fetchall()
            if rows:
                return [_row_to_dict(r) for r in rows]
            # Fallback: LIKE search on name + properties
            pat = f"%{query}%"
            rows = con.execute(
                "SELECT * FROM nodes WHERE name LIKE ? OR properties LIKE ? LIMIT ?",
                (pat, pat, limit),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            con.close()


def _all_nodes(limit: int = 50) -> list[dict]:
    with _lock:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT * FROM nodes ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            con.close()


# ── Prompt formatting ──────────────────────────────────────────────────────────

_TYPE_PRIORITY = {
    "identity": 0, "person": 1, "preference": 2, "project": 3,
    "device": 4, "place": 5, "skill": 6, "event": 7, "fact": 8,
}

def format_for_prompt(budget: int = 900) -> str:
    """
    Build a compact memory block for the system prompt.
    Shows the most important / recently updated nodes first.
    Relationships (edges) are inline as 'type: name [rel → neighbour]'.
    """
    nodes = _all_nodes(limit=200)
    if not nodes:
        return ""

    nodes.sort(key=lambda n: (
        _TYPE_PRIORITY.get(n["type"], 9),
        -(len(n["updated_at"] or "")),
    ))

    lines: list[str] = []
    used = 0
    overflow_names: list[str] = []

    for node in nodes:
        name  = node["name"]
        ntype = node["type"]
        props = node.get("properties") or {}
        value = props.get("value") or props.get("summary") or ""
        rel_parts: list[str] = []
        nbrs = neighbors(node["id"], depth=1, direction="out")
        for nb in nbrs[:3]:
            rel_parts.append(f"{nb['predicate']} → {nb['name']}")

        line = f"  [{ntype}] {name}"
        if value:
            line += f": {str(value)[:120]}"
        if rel_parts:
            line += f" ({', '.join(rel_parts)})"

        if used + len(line) + 1 <= budget:
            lines.append(line)
            used += len(line) + 1
        else:
            overflow_names.append(name)

    if not lines:
        return ""

    out = ["[KNOWLEDGE GRAPH — entities and relationships I know about this person and their world]"]
    out.extend(lines)

    if overflow_names:
        idx = ", ".join(overflow_names[:20])
        if len(overflow_names) > 20:
            idx += f" (+{len(overflow_names)-20} more)"
        out.append("")
        out.append(f"[ALSO KNOWN — call recall_memory to expand any of these: {idx}]")

    return "\n".join(out) + "\n"


# ── Import from flat memory ────────────────────────────────────────────────────

_CAT_TO_TYPE = {
    "identity":      "identity",
    "preferences":   "preference",
    "projects":      "project",
    "relationships": "person",
    "wishes":        "fact",
    "notes":         "fact",
}


def import_flat_memory(flat: dict) -> int:
    """
    Seed the graph from the old long_term.json dict.
    Returns number of nodes created.
    """
    created = 0
    for cat, items in flat.items():
        if cat == "sessions" or not isinstance(items, dict):
            continue
        ntype = _CAT_TO_TYPE.get(cat, "fact")
        for key, entry in items.items():
            if isinstance(entry, dict):
                value = entry.get("value", "")
            else:
                value = str(entry or "")
            if not value:
                continue
            # For relationships, the key is the person's name
            node_name = key.replace("_", " ").title()
            add_node(node_name, ntype, value=value, source_category=cat, source_key=key)
            created += 1
    print(f"[GraphMemory] 📥 Imported {created} nodes from flat memory.")
    return created


# ── Compatibility shims (drop-in for memory_manager.py callers) ───────────────

def remember(key: str, value: str, category: str = "notes") -> str:
    type_ = _CAT_TO_TYPE.get(category, "fact")
    name  = key.replace("_", " ").title()
    nid   = add_node(name, type_, value=value, source_category=category)
    return f"Remembered: [{type_}] {name} = {value[:80]}"


def forget(key: str, category: str = "notes") -> str:
    name = key.replace("_", " ").title()
    if delete_node(name):
        return f"Forgotten: {name}"
    return f"Not found: {name}"


forget_memory = forget


def search_memory(query: str, limit: int = 8) -> str:
    if not query:
        nodes = _all_nodes(limit)
    else:
        nodes = search(query, limit)
    if not nodes:
        return f"Nothing stored about '{query}'." if query else "No memories stored yet."
    lines = []
    for n in nodes:
        props = n.get("properties") or {}
        val   = props.get("value") or props.get("summary") or str(props)[:80]
        lines.append(f"[{n['type']}] {n['name']}: {val}")
    head = f"Graph memory matching '{query}':" if query else "Everything I know:"
    return head + "\n" + "\n".join(lines)


def all_entries_for_ui() -> list[dict]:
    """Flat list for the memory panel UI, newest first."""
    nodes = _all_nodes(limit=200)
    rows  = []
    for n in nodes:
        props = n.get("properties") or {}
        val   = props.get("value") or props.get("summary") or str(props)[:120]
        rows.append({
            "category": n["type"],
            "key":      n["name"],
            "value":    val,
            "updated":  (n.get("updated_at") or "")[:10],
        })
    rows.sort(key=lambda r: (r["updated"] or "0000-00-00"), reverse=True)
    return rows


# ── Stats ──────────────────────────────────────────────────────────────────────

def stats() -> dict:
    with _lock:
        con = _connect()
        try:
            n_nodes = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            n_edges = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            types   = con.execute(
                "SELECT type, COUNT(*) as c FROM nodes GROUP BY type ORDER BY c DESC"
            ).fetchall()
            return {
                "nodes": n_nodes,
                "edges": n_edges,
                "by_type": {r[0]: r[1] for r in types},
            }
        finally:
            con.close()


# ── Auto-seed from existing long_term.json on first run ───────────────────────

def _auto_seed() -> None:
    """If DB is empty and long_term.json has data, import it once."""
    try:
        with _lock:
            con = _connect()
            count = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            con.close()
        if count > 0:
            return
        lt_path = _base() / "memory" / "long_term.json"
        if not lt_path.exists():
            return
        flat = json.loads(lt_path.read_text(encoding="utf-8"))
        if flat:
            import_flat_memory(flat)
    except Exception as e:
        print(f"[GraphMemory] ⚠️ Auto-seed failed: {e}")


_auto_seed()
