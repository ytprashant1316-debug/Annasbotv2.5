# database.py

import sqlite3
import os
from datetime import datetime
from typing import Optional, Dict, Any

DB_PATH = os.path.join(os.path.dirname(__file__), "books_cache.db")


def get_connection():
    """Get database connection with row factory for dict-like access."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_database():
    """Initialize the database schema."""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS books (
            md5 TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            author TEXT,
            format TEXT,
            size TEXT,
            year TEXT,
            file_path TEXT,
            downloaded_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Create index for faster lookups
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_md5 ON books(md5)
    """)
    
    conn.commit()
    conn.close()


def check_book_exists(md5: str) -> Optional[Dict[str, Any]]:
    """
    Check if a book exists in the local database by MD5.
    Returns book info dict if found, None otherwise.
    """
    md5 = md5.lower()
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM books WHERE md5 = ?", (md5,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return dict(row)
    return None


def save_book(md5: str, title: str, author: str = None, format_: str = None, 
              size: str = None, year: str = None, file_path: str = None):
    """
    Save or update a book in the database.
    If file_path is provided, mark as downloaded.
    """
    md5 = md5.lower()
    conn = get_connection()
    cursor = conn.cursor()
    
    now = datetime.now().isoformat()
    
    cursor.execute("""
        INSERT OR REPLACE INTO books 
        (md5, title, author, format, size, year, file_path, downloaded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (md5, title, author, format_, size, year, file_path, now))
    
    conn.commit()
    conn.close()


def update_download_path(md5: str, file_path: str):
    """Update the file path after successful download."""
    md5 = md5.lower()
    conn = get_connection()
    cursor = conn.cursor()
    
    now = datetime.now().isoformat()
    cursor.execute(
        "UPDATE books SET file_path = ?, downloaded_at = ? WHERE md5 = ?",
        (file_path, now, md5)
    )
    
    conn.commit()
    conn.close()


def get_local_file_path(md5: str) -> Optional[str]:
    """Get the local file path for a downloaded book."""
    book = check_book_exists(md5)
    if book and book.get("file_path"):
        return book["file_path"]
    return None


def search_local_books(query: str = None, limit: int = 50) -> list:
    """
    Search local database for books.
    If query is provided, search in title/author.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    if query:
        search_term = f"%{query}%"
        cursor.execute("""
            SELECT * FROM books 
            WHERE title LIKE ? OR author LIKE ?
            ORDER BY downloaded_at DESC
            LIMIT ?
        """, (search_term, search_term, limit))
    else:
        cursor.execute("""
            SELECT * FROM books 
            ORDER BY downloaded_at DESC
            LIMIT ?
        """, (limit,))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


# Initialize database on module import
if __name__ == "__main__":
    init_database()
    print(f"Database initialized at: {DB_PATH}")

