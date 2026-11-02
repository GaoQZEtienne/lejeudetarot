import json
import sqlite3
from pathlib import Path


class Storage:
    def __init__(self, path):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS room (id INTEGER PRIMARY KEY, snapshot TEXT NOT NULL)')
        self.db.commit()

    def load(self):
        row = self.db.execute('SELECT snapshot FROM room WHERE id=1').fetchone()
        return json.loads(row[0]) if row else None

    def save(self, data):
        with self.db:
            self.db.execute('INSERT INTO room(id,snapshot) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET snapshot=excluded.snapshot',
                            (json.dumps(data, ensure_ascii=False),))

    def close(self):
        self.db.close()
