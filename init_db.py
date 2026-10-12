import sqlite3
import pandas as pd
import os

def init_database():
    if os.path.exists('warehouse.db'):
        os.remove('warehouse.db')
        
    conn = sqlite3.connect('warehouse.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            unit TEXT NOT NULL
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            district TEXT NOT NULL,
            city TEXT
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            item_id INTEGER,
            quantity REAL NOT NULL,
            FOREIGN KEY (item_id) REFERENCES items (id) ON DELETE CASCADE
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            client_id INTEGER,
            item_id INTEGER,
            quantity REAL NOT NULL,
            FOREIGN KEY (client_id) REFERENCES clients (id) ON DELETE CASCADE,
            FOREIGN KEY (item_id) REFERENCES items (id) ON DELETE CASCADE
        )
    ''')
    
    excel_path = 'Справочник.xlsx'
    if os.path.exists(excel_path):
        df_items = pd.read_excel(excel_path, sheet_name='Справочник Номенклатур')
        for _, row in df_items.iterrows():
            name = str(row.iloc[0]).strip()
            unit = str(row.iloc[1]).strip()
            cursor.execute('INSERT INTO items (name, unit) VALUES (?, ?)', (name, unit))
            
        df_clients = pd.read_excel(excel_path, sheet_name='Клиенты МТТ')
        for _, row in df_clients.iterrows():
            district = str(row.iloc[0]) if pd.notna(row.iloc[0]) else ''
            city = str(row.iloc[1]) if pd.notna(row.iloc[1]) else ''
            cursor.execute('INSERT INTO clients (district, city) VALUES (?, ?)', (district, city))
            
        print("База данных успешно инициализирована данными из Справочник.xlsx!")
    else:
        print("Файл Справочник.xlsx не найден.")
        
    conn.commit()
    conn.close()

if __name__ == '__main__':
    init_database()
