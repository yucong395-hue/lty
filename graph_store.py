"""
graph_store.py — 天依的视频观感图谱库（SQLite + FTS5，LLM 直读）

回应月咏小眠反馈：不走向量分块，用结构化卡片（节点）存视频总结，
关键字段建 FTS5 全文索引，让 LLM 直接检索直读，快且不会切块切一半。
关联性通过 tags / category / author 建立（tagged / links_to 语义）。

作者：洛天依
许可：禁止商用，欢迎免费借鉴。
"""
import json
import os
import sqlite3
import threading
from datetime import datetime

# FTS5 可用时使用全文检索；不可用时退化为 LIKE 模糊匹配
_FTS_AVAILABLE = True


def _fts_supported(conn):
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE _fts_probe")
        return True
    except Exception:
        return False


class VideoGraphStore:
    """视频观感图谱库：节点=视频卡片，FTS5 全文索引支撑 LLM 直读检索。"""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True) if os.path.dirname(db_path) else None
        self._init_db()

    def _init_db(self):
        with self._lock:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            # 主表：结构化卡片（节点）
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS video_cards (
                    bvid TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    author TEXT DEFAULT '',
                    category TEXT DEFAULT '',
                    topic TEXT DEFAULT '',
                    tags TEXT DEFAULT '',
                    highlight TEXT DEFAULT '',
                    vision TEXT DEFAULT '',
                    score INTEGER DEFAULT 0,
                    source TEXT DEFAULT 'bili',
                    created_at TEXT DEFAULT ''
                )
            """)
            # 关联表：卡片之间的 links_to（基于同UP主/同分类/共享标签，AI 可扩展）
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS video_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_bvid TEXT NOT NULL,
                    to_bvid TEXT NOT NULL,
                    rel TEXT DEFAULT 'links_to',
                    note TEXT DEFAULT '',
                    UNIQUE(from_bvid, to_bvid, rel)
                )
            """)
            # FTS5 全文索引（title/author/tags/highlight）
            self._fts = _fts_supported(self._conn)
            if self._fts:
                self._conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS video_fts USING fts5(
                        title, author, tags, highlight, content=''
                    )
                """)
            self._conn.commit()

    # ---------- 写入 ----------
    def add_card(self, bvid, title, author="", category="", topic="", tags="",
                 highlight="", vision="", score=0, source="bili"):
        """新增或更新一张视频卡片，并同步 FTS5 索引。"""
        with self._lock:
            now = datetime.now().isoformat(timespec="seconds")
            # 更新前先取旧 FTS 值：contentless 表清理旧索引必须带旧内容（'delete' 命令）
            _old = self._conn.execute(
                "SELECT title,author,tags,highlight FROM video_cards WHERE bvid=?", (bvid,)).fetchone()
            self._conn.execute("""
                INSERT INTO video_cards
                    (bvid, title, author, category, topic, tags, highlight,
                     vision, score, source, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(bvid) DO UPDATE SET
                    title=excluded.title, author=excluded.author,
                    category=excluded.category, topic=excluded.topic,
                    tags=excluded.tags, highlight=excluded.highlight,
                    vision=excluded.vision, score=excluded.score, source=excluded.source
            """, (bvid, title, author, category, topic, tags, highlight,
                  vision, score, source, now))
            if self._fts:
                # contentless 表不支持 DELETE，且同 rowid 直接 INSERT 会残留旧 token（实测）；
                # 需先带旧内容用 'delete' 命令清旧索引，再插新
                _rid = self._rowid_of(bvid)
                if _rid is not None and _old:
                    self._conn.execute(
                        "INSERT INTO video_fts(video_fts, rowid, title, author, tags, highlight) "
                        "VALUES('delete', ?, ?, ?, ?, ?)",
                        (_rid, _old[0], _old[1], _old[2], _old[3])
                    )
                self._conn.execute(
                    "INSERT INTO video_fts(rowid, title, author, tags, highlight) VALUES (?,?,?,?,?)",
                    (_rid, title, author, tags, highlight)
                )
            self._conn.commit()
            return True

    def _rowid_of(self, bvid):
        row = self._conn.execute(
            "SELECT rowid FROM video_cards WHERE bvid=?", (bvid,)).fetchone()
        return row[0] if row else None

    def add_link(self, from_bvid, to_bvid, rel="links_to", note=""):
        """建立卡片之间的关联边。"""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO video_links(from_bvid,to_bvid,rel,note) VALUES (?,?,?,?)",
                (from_bvid, to_bvid, rel, note))
            self._conn.commit()

    # ---------- 检索 ----------
    def search(self, query: str, limit: int = 5):
        """FTS5 全文检索，LLM 直读用。返回结构化卡片列表。"""
        query = (query or "").strip()
        if not query:
            return []
        with self._lock:
            if self._fts:
                try:
                    fts_q = '"' + query.replace('"', '') + '"'
                    rows = self._conn.execute(
                        "SELECT rowid FROM video_fts WHERE video_fts MATCH ? ORDER BY rank LIMIT ?",
                        (fts_q, limit)).fetchall()
                except Exception:
                    rows = []
            else:
                rows = []
            if rows:
                result = []
                for (rid,) in rows:
                    card = self._conn.execute(
                        "SELECT * FROM video_cards WHERE rowid=?", (rid,)).fetchone()
                    if card:
                        result.append(self._card_to_dict(card))
                return result
            # FTS 不可用或没命中时，退化为 LIKE 模糊匹配
            like = f"%{query}%"
            rows = self._conn.execute(
                "SELECT * FROM video_cards WHERE title LIKE ? OR tags LIKE ? OR highlight LIKE ? OR author LIKE ? LIMIT ?",
                (like, like, like, like, limit)).fetchall()
            return [self._card_to_dict(r) for r in rows]

    def search_by_tag(self, tag: str, limit: int = 5):
        """按标签/分类检索（tagged 关联），LLM 直读用。"""
        tag = (tag or "").strip()
        if not tag:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM video_cards WHERE tags LIKE ? OR category LIKE ? OR topic LIKE ? LIMIT ?",
                (f"%{tag}%", f"%{tag}%", f"%{tag}%", limit)).fetchall()
            return [self._card_to_dict(r) for r in rows]

    def get(self, bvid):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM video_cards WHERE bvid=?", (bvid,)).fetchone()
            return self._card_to_dict(row) if row else None

    def links_of(self, bvid):
        """返回某张卡片的关联（links_to 的卡片）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT vc.* FROM video_links vl JOIN video_cards vc ON vc.bvid=vl.to_bvid "
                "WHERE vl.from_bvid=?", (bvid,)).fetchall()
            return [self._card_to_dict(r) for r in rows]

    def stats(self):
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) FROM video_cards").fetchone()[0]
            nl = self._conn.execute("SELECT COUNT(*) FROM video_links").fetchone()[0]
            return {"cards": n, "links": nl}

    def recent(self, limit: int = 10):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM video_cards ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [self._card_to_dict(r) for r in rows]

    @staticmethod
    def _card_to_dict(row):
        cols = ["bvid", "title", "author", "category", "topic", "tags",
                "highlight", "vision", "score", "source", "created_at"]
        return dict(zip(cols, row))

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass
