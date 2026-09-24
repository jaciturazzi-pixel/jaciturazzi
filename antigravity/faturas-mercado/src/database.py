import sqlite3
from datetime import datetime
from typing import List, Dict, Tuple, Optional
import os

class Database:
    def __init__(self, db_path: str = "data/faturas.db"):
        # Ensure the directory exists
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        cursor = self.conn.cursor()
        
        # Table for Invoices
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            invoice_number TEXT UNIQUE,
            invoice_date DATE,
            nif TEXT,
            total_amount REAL,
            discount_amount REAL DEFAULT 0,
            pdf_path TEXT,
            email_id TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        # Table for Line Items
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS line_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL,
            description TEXT NOT NULL,
            quantity REAL NOT NULL,
            unit TEXT,
            unit_price REAL,
            total_price REAL,
            discount REAL DEFAULT 0,
            category TEXT,
            FOREIGN KEY (invoice_id) REFERENCES invoices (id)
        )
        ''')

        # Table for Category Cache (crucial for avoiding AI calls)
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS category_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT UNIQUE NOT NULL,
            category TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        self.conn.commit()

    def invoice_exists(self, invoice_number: str) -> bool:
        cursor = self.conn.cursor()
        cursor.execute("SELECT 1 FROM invoices WHERE invoice_number = ?", (invoice_number,))
        return cursor.fetchone() is not None

    def email_processed(self, email_id: str) -> bool:
        cursor = self.conn.cursor()
        cursor.execute("SELECT 1 FROM invoices WHERE email_id = ?", (email_id,))
        return cursor.fetchone() is not None

    def insert_invoice(self, data: dict) -> int:
        cursor = self.conn.cursor()
        cursor.execute('''
        INSERT INTO invoices (store, invoice_number, invoice_date, nif, total_amount, discount_amount, pdf_path, email_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data.get('store'),
            data.get('invoice_number'),
            data.get('invoice_date'),
            data.get('nif'),
            data.get('total_amount'),
            data.get('discount_amount', 0),
            data.get('pdf_path'),
            data.get('email_id')
        ))
        invoice_id = cursor.lastrowid
        self.conn.commit()
        return invoice_id

    def insert_line_items(self, invoice_id: int, items: List[dict]):
        cursor = self.conn.cursor()
        for item in items:
            cursor.execute('''
            INSERT INTO line_items (invoice_id, description, quantity, unit, unit_price, total_price, discount, category)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                invoice_id,
                item.get('description'),
                item.get('quantity'),
                item.get('unit'),
                item.get('unit_price'),
                item.get('total_price'),
                item.get('discount', 0),
                item.get('category')
            ))
        self.conn.commit()

    # --- CATEGORY CACHE METHODS ---

    def get_cached_category(self, product_name: str) -> Optional[str]:
        """Returns the category for a given product name, if it exists in the cache."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT category FROM category_cache WHERE product_name = ?", (product_name,))
        row = cursor.fetchone()
        return row['category'] if row else None
    
    def get_uncategorized_items(self) -> List[Tuple[int, str]]:
        """Returns line items that don't have a category assigned yet."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT id, description FROM line_items WHERE category IS NULL")
        return cursor.fetchall()
        
    def update_item_category(self, item_id: int, category: str):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE line_items SET category = ? WHERE id = ?", (category, item_id))
        self.conn.commit()

    def cache_category(self, product_name: str, category: str):
        """Saves a category assignment into the cache to avoid future AI calls."""
        cursor = self.conn.cursor()
        cursor.execute('''
        INSERT OR REPLACE INTO category_cache (product_name, category)
        VALUES (?, ?)
        ''', (product_name, category))
        self.conn.commit()

    def get_outros_items(self):
        """Returns line items that are categorized as 'Outros' for reclassification."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT id, description FROM line_items WHERE category = 'Outros'")
        return cursor.fetchall()

    def clear_outros_cache(self):
        """Remove cached categories marked as 'Outros' to force reclassification."""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM category_cache WHERE category = 'Outros'")
        deleted = cursor.rowcount
        self.conn.commit()
        return deleted

    def close(self):
        self.conn.close()
