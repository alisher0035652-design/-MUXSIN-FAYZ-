import os
import io
from datetime import datetime
import pandas as pd
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, session

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'warehouse_super_secret_key_1c')

ORGANIZATIONS = {
    "OTABEK UMAR FAYZ": {
        "name": "OTABEK UMAR FAYZ",
        "address": "Бекобод тумани",
        "account": "20208000907017874001",
        "bank": "ТИФ \"Миллий банк\" Бекобод БХМ.",
        "mfo": "00450",
        "inn": "311147751",
        "has_vat": False  # БЕЗ НДС
    },
    "ISMOIL-OMAD-FAYZ-555": {
        "name": "ISMOIL-OMAD-FAYZ-555",
        "address": "Бекобод тумани",
        "account": "20208000805690249001",
        "bank": "ТИФ \"Миллий банк\" Бекобод БХМ",
        "mfo": "00450",
        "inn": "310748843",
        "has_vat": True   # С НДС (12%)
    }
}

@app.template_filter('money')
def format_money(value):
    try:
        val = float(value)
        return f"{val:,.2f}".replace(',', ' ')
    except (ValueError, TypeError):
        return "0.00"

@app.template_filter('date_ru')
def format_date_ru(value):
    if not value:
        return ""
    try:
        if isinstance(value, str):
            dt = datetime.strptime(value.split('T')[0], '%Y-%m-%d')
        else:
            dt = value
        return dt.strftime('%d.%m.%Y')
    except (ValueError, TypeError):
        return str(value)

def get_db_connection():
    database_url = os.environ.get('DATABASE_URL')
    if database_url and database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    
    if database_url:
        conn = psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        import sqlite3
        conn = sqlite3.connect('warehouse.db')
        conn.row_factory = sqlite3.Row
    return conn

def init_db_schema():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS items (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                unit VARCHAR(50) NOT NULL,
                organization VARCHAR(100) DEFAULT 'OTABEK UMAR FAYZ'
            );
        ''')
        cursor.execute('''
            ALTER TABLE items ADD COLUMN IF NOT EXISTS organization VARCHAR(100) DEFAULT 'OTABEK UMAR FAYZ';
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS item_prices (
                id SERIAL PRIMARY KEY,
                item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
                price NUMERIC(12, 2) NOT NULL,
                effective_date DATE NOT NULL
            );
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS clients (
                id SERIAL PRIMARY KEY,
                category VARCHAR(255) NOT NULL,
                client_number INTEGER NOT NULL,
                UNIQUE(category, client_number)
            );
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sales_docs (
                id SERIAL PRIMARY KEY,
                doc_number VARCHAR(50) NOT NULL UNIQUE,
                date DATE NOT NULL,
                client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
                organization VARCHAR(100) DEFAULT 'OTABEK UMAR FAYZ'
            );
        ''')
        cursor.execute('''
            ALTER TABLE sales_docs ADD COLUMN IF NOT EXISTS organization VARCHAR(100) DEFAULT 'OTABEK UMAR FAYZ';
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sales_items (
                id SERIAL PRIMARY KEY,
                doc_id INTEGER REFERENCES sales_docs(id) ON DELETE CASCADE,
                item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
                quantity NUMERIC(12, 2) NOT NULL
            );
        ''')
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Schema init error: {e}")
    finally:
        cursor.close()
        conn.close()

init_db_schema()

def get_price_on_date(cursor, item_id, doc_date):
    cursor.execute('''
        SELECT price FROM item_prices 
        WHERE item_id = %s AND effective_date <= %s 
        ORDER BY effective_date DESC, id DESC LIMIT 1
    ''', (item_id, doc_date))
    row = cursor.fetchone()
    if row:
        return float(row['price'] if isinstance(row, dict) else row[0])
    
    cursor.execute('''
        SELECT price FROM item_prices 
        WHERE item_id = %s 
        ORDER BY effective_date ASC, id ASC LIMIT 1
    ''', (item_id,))
    row = cursor.fetchone()
    if row:
        return float(row['price'] if isinstance(row, dict) else row[0])
    return 0.0

def generate_next_doc_number(cursor):
    try:
        cursor.execute("SELECT doc_number FROM sales_docs ORDER BY id DESC LIMIT 1")
        last_doc = cursor.fetchone()
        if not last_doc:
            return "001001"
        
        doc_num_val = last_doc['doc_number'] if isinstance(last_doc, dict) else last_doc[0]
        if not doc_num_val:
            return "001001"
            
        digits = ''.join(filter(str.isdigit, str(doc_num_val)))
        if not digits:
            return "001001"
            
        next_num = int(digits) + 1
        return f"{next_num:06d}"
    except Exception:
        return "001001"

@app.route('/')
def index():
    session.pop('current_org', None)
    return render_template('org_select.html')

@app.route('/select_org/<org_name>')
def select_org(org_name):
    if org_name in ORGANIZATIONS:
        session['current_org'] = org_name
    return redirect(url_for('sales'))

@app.route('/sales', methods=['GET', 'POST'])
def sales():
    current_org = session.get('current_org')
    if not current_org or current_org not in ORGANIZATIONS:
        return redirect(url_for('index'))

    conn = get_db_connection()
    cursor = conn.cursor()
    
    if request.method == 'POST':
        try:
            date = request.form.get('date')
            client_id_raw = request.form.get('client_id')
            
            if not date or not client_id_raw:
                flash('Заполните дату и выберите МТТ!', 'danger')
                return redirect(url_for('sales'))

            client_id = int(client_id_raw)
            item_ids = request.form.getlist('item_id[]')
            quantities = request.form.getlist('quantity[]')

            if not item_ids or len(item_ids) == 0:
                flash('Добавьте хотя бы одну позицию товара!', 'warning')
                return redirect(url_for('sales'))

            doc_number = generate_next_doc_number(cursor)

            cursor.execute(
                "INSERT INTO sales_docs (doc_number, date, client_id, organization) VALUES (%s, %s, %s, %s) RETURNING id",
                (doc_number, date, client_id, current_org)
            )
            row = cursor.fetchone()
            doc_id = row['id'] if isinstance(row, dict) else row[0]

            for i in range(len(item_ids)):
                if i < len(quantities) and quantities[i]:
                    qty_str = str(quantities[i]).strip()
                    if qty_str:
                        item_id = int(item_ids[i])
                        qty = float(qty_str)
                        cursor.execute(
                            "INSERT INTO sales_items (doc_id, item_id, quantity) VALUES (%s, %s, %s)",
                            (doc_id, item_id, qty)
                        )

            conn.commit()
            flash(f'Накладная № {doc_number} успешно проведена!', 'success')
        except Exception as e:
            conn.rollback()
            flash(f'Ошибка при создании отгрузки: {e}', 'danger')
        finally:
            cursor.close()
            conn.close()
        return redirect(url_for('sales'))

    filter_date = request.args.get('filter_date', '').strip()
    filter_client = request.args.get('filter_client', '').strip()
    filter_doc = request.args.get('filter_doc', '').strip()

    query = '''
        SELECT d.id, d.doc_number, d.date, c.category, c.client_number, d.organization,
               COALESCE((SELECT STRING_AGG(i.name || ' (' || TO_CHAR(si.quantity, 'FM999999999.00') || ' ' || i.unit || ')', ', ') 
                FROM sales_items si JOIN items i ON si.item_id = i.id WHERE si.doc_id = d.id), '') as items_summary
        FROM sales_docs d
        JOIN clients c ON d.client_id = c.id
        WHERE d.organization = %s
    '''
    params = [current_org]

    if filter_date:
        query += " AND d.date = %s"
        params.append(filter_date)

    if filter_client:
        query += " AND d.client_id = %s"
        params.append(int(filter_client))

    if filter_doc:
        query += " AND d.doc_number ILIKE %s"
        params.append(f"%{filter_doc}%")

    query += " ORDER BY d.date DESC, d.id DESC"

    cursor.execute(query, params)
    sales_docs = cursor.fetchall()

    cursor.execute("SELECT * FROM items WHERE organization = %s ORDER BY name", (current_org,))
    items = cursor.fetchall()

    cursor.execute("SELECT * FROM clients ORDER BY category, client_number")
    clients = cursor.fetchall()
    
    cursor.close()
    conn.close()

    return render_template('sales.html', 
                           sales_docs=sales_docs, 
                           items=items, 
                           clients=clients, 
                           current_org=current_org,
                           filter_date=filter_date, 
                           filter_client=filter_client,
                           filter_doc=filter_doc)

@app.route('/sales/edit/<int:doc_id>', methods=['GET', 'POST'])
def edit_sale(doc_id):
    current_org = session.get('current_org')
    if not current_org:
        return redirect(url_for('index'))

    conn = get_db_connection()
    cursor = conn.cursor()
    
    if request.method == 'POST':
        try:
            date = request.form.get('date')
            client_id = int(request.form.get('client_id'))
            
            item_ids = request.form.getlist('item_id[]')
            quantities = request.form.getlist('quantity[]')

            cursor.execute(
                "UPDATE sales_docs SET date = %s, client_id = %s, organization = %s WHERE id = %s",
                (date, client_id, current_org, doc_id)
            )

            cursor.execute("DELETE FROM sales_items WHERE doc_id = %s", (doc_id,))

            for i in range(len(item_ids)):
                if i < len(quantities) and quantities[i]:
                    qty_str = str(quantities[i]).strip()
                    if qty_str:
                        item_id = int(item_ids[i])
                        qty = float(qty_str)
                        cursor.execute(
                            "INSERT INTO sales_items (doc_id, item_id, quantity) VALUES (%s, %s, %s)",
                            (doc_id, item_id, qty)
                        )

            conn.commit()
            flash('Накладная успешно обновлена!', 'success')
        except Exception as e:
            conn.rollback()
            flash(f'Ошибка при обновлении накладной: {e}', 'danger')
        finally:
            cursor.close()
            conn.close()
        return redirect(url_for('sales'))

    cursor.execute("SELECT * FROM sales_docs WHERE id = %s", (doc_id,))
    doc = cursor.fetchone()
    if not doc:
        cursor.close()
        conn.close()
        flash('Накладная не найдена', 'danger')
        return redirect(url_for('sales'))

    cursor.execute("SELECT * FROM sales_items WHERE doc_id = %s", (doc_id,))
    sale_items = cursor.fetchall()

    cursor.execute("SELECT * FROM items WHERE organization = %s ORDER BY name", (current_org,))
    items = cursor.fetchall()

    cursor.execute("SELECT * FROM clients ORDER BY category, client_number")
    clients = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template('edit_sale.html', doc=doc, sale_items=sale_items, items=items, clients=clients, current_org=current_org)

@app.route('/sales/print/<int:doc_id>')
def print_sale_route(doc_id):
    show_prices = request.args.get('show_prices') != '0'
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT d.id, d.doc_number, d.date, c.category, c.client_number, d.organization
        FROM sales_docs d
        JOIN clients c ON d.client_id = c.id
        WHERE d.id = %s
    ''', (doc_id,))
    doc = cursor.fetchone()

    if not doc:
        cursor.close()
        conn.close()
        flash('Накладная не найдена', 'danger')
        return redirect(url_for('sales'))

    org_key = doc['organization'] if doc['organization'] in ORGANIZATIONS else "OTABEK UMAR FAYZ"
    org_data = ORGANIZATIONS[org_key]

    cursor.execute('''
        SELECT i.id as item_id, i.name as item_name, i.unit, si.quantity
        FROM sales_items si
        JOIN items i ON si.item_id = i.id
        WHERE si.doc_id = %s
    ''', (doc_id,))
    raw_items = cursor.fetchall()

    doc_date = doc['date']
    processed_items = []
    total_sum = 0.0
    total_vat = 0.0
    total_with_vat = 0.0

    has_vat = org_data.get('has_vat', False)

    for r in raw_items:
        qty = float(r['quantity'])
        price = get_price_on_date(cursor, r['item_id'], doc_date)
        sum_supply = qty * price

        if has_vat:
            vat = sum_supply * 0.12
            sum_with_vat = sum_supply + vat
        else:
            vat = 0.0
            sum_with_vat = sum_supply

        total_sum += sum_supply
        total_vat += vat
        total_with_vat += sum_with_vat

        processed_items.append({
            'item_name': r['item_name'],
            'unit': r['unit'],
            'quantity': qty,
            'price': price,
            'sum_supply': sum_supply,
            'vat': vat,
            'sum_with_vat': sum_with_vat
        })

    cursor.close()
    conn.close()
    
    totals = {
        'sum_supply': total_sum,
        'vat': total_vat,
        'sum_with_vat': total_with_vat
    }

    return render_template('print_sale.html', doc=doc, org=org_data, items=processed_items, totals=totals, show_prices=show_prices, has_vat=has_vat)

@app.route('/sales/export_excel')
def export_sales
