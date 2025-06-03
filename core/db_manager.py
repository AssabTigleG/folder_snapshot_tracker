# core/db_manager.py
import sqlite3
import json
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

DB_FILE = "tracker_data.db"

class DatabaseManager:
    def __init__(self, db_path=DB_FILE):
        self.db_path = db_path
        self._create_tables_if_not_exist()

    def _get_connection(self):
        return sqlite3.connect(self.db_path)

    def _create_tables_if_not_exist(self):
        conn = self._get_connection()
        cursor = conn.cursor()
        # Snapshots table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                root_folders TEXT NOT NULL, -- JSON list of root folder paths
                description TEXT
            )
        """)
        # Snapshot items table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS snapshot_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL,
                root_folder_idx INTEGER NOT NULL, -- Index in snapshots.root_folders
                relative_path TEXT NOT NULL,
                item_name TEXT NOT NULL,
                is_file INTEGER NOT NULL, -- 0 for folder, 1 for file
                size INTEGER, -- NULL for folders
                lmt REAL, -- Last Modified Timestamp
                ct REAL, -- Creation Timestamp
                content_hash TEXT, -- NULL for folders or if hashing failed
                FOREIGN KEY (snapshot_id) REFERENCES snapshots (id) ON DELETE CASCADE,
                UNIQUE (snapshot_id, root_folder_idx, relative_path)
            )
        """)
        conn.commit()
        conn.close()

    def add_snapshot_record(self, name: str, timestamp: str, root_folders: List[str], description: Optional[str] = None) -> int:
        conn = self._get_connection()
        cursor = conn.cursor()
        root_folders_json = json.dumps([str(Path(p)) for p in root_folders]) # Ensure paths are strings
        cursor.execute(
            "INSERT INTO snapshots (name, timestamp, root_folders, description) VALUES (?, ?, ?, ?)",
            (name, timestamp, root_folders_json, description)
        )
        snapshot_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return snapshot_id

    def batch_insert_snapshot_items(self, snapshot_id: int, items: List[Dict[str, Any]]):
        conn = self._get_connection()
        cursor = conn.cursor()
        # Ensure correct order of fields for insertion
        items_data = [
            (
                snapshot_id,
                item['root_folder_idx'],
                item['relative_path'],
                item['item_name'],
                1 if item['is_file'] else 0,
                item.get('size'),
                item.get('lmt'),
                item.get('ct'),
                item.get('content_hash')
            ) for item in items
        ]
        try:
            cursor.executemany(
                """INSERT INTO snapshot_items 
                   (snapshot_id, root_folder_idx, relative_path, item_name, is_file, size, lmt, ct, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                items_data
            )
            conn.commit()
        except sqlite3.Error as e:
            print(f"Database error during batch insert: {e}")
            conn.rollback()
        finally:
            conn.close()

    def get_all_snapshots(self) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, timestamp, root_folders, description FROM snapshots ORDER BY timestamp DESC")
        snapshots = []
        for row in cursor.fetchall():
            snapshots.append({
                'id': row[0],
                'name': row[1],
                'timestamp': row[2],
                'root_folders': json.loads(row[3]),
                'description': row[4]
            })
        conn.close()
        return snapshots

    def get_snapshot_items(self, snapshot_id: int) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT root_folder_idx, relative_path, item_name, is_file, size, lmt, ct, content_hash FROM snapshot_items WHERE snapshot_id = ?",
            (snapshot_id,)
        )
        items = []
        for row in cursor.fetchall():
            items.append({
                'root_folder_idx': row[0],
                'relative_path': row[1],
                'item_name': row[2],
                'is_file': bool(row[3]),
                'size': row[4],
                'lmt': row[5],
                'ct': row[6],
                'content_hash': row[7]
            })
        conn.close()
        return items

    def delete_snapshot(self, snapshot_id: int):
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            # Cascading delete should handle snapshot_items
            cursor.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))
            conn.commit()
        except sqlite3.Error as e:
            print(f"Error deleting snapshot {snapshot_id}: {e}")
            conn.rollback()
        finally:
            conn.close()