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
        "has_vat": False,  # БЕЗ НДС
        "pin": "2225"      # 4-значный ПИН-код для входа
    },
    "ISMOIL-OMAD-FAYZ-555": {
        "name": "ISMOIL-OMAD-FAYZ-555",
        "address": "Бекобод тумани",
        "account": "20208000805690249001",
        "bank": "ТИФ \"Миллий банк\" Бекобод БХМ",
        "mfo": "00450",
        "inn": "310748843",
        "has_vat": True,   # С НДС (12%)
        "pin": "1111"      # 4-значный ПИН-код для входа
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

@app.route('/org_login/<org_name>', methods=['GET', 'POST'])
def org_login(org_name):
    if org_name not in ORGANIZATIONS:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        entered_pin = request.form.get('pin', '').strip()
        if entered_pin == ORGANIZATIONS[org_name]['pin']:
            session['current_org'] = org_name
            flash(f'Успешный вход в организацию {org_name}', 'success')
            return redirect(url_for('sales'))
        else:
            flash('Неверный 4-значный ПИН-код!', 'danger')
            
    return render_template('org_login.html', org_name=org_name)

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
def export_sales_excel():
    current_org = session.get('current_org', 'OTABEK UMAR FAYZ')
    conn = get_db_connection()
    cursor = conn.cursor()
    
    query = '''
        SELECT d.doc_number, d.date, 
               c.category || ' — МТТ №' || c.client_number as client_name,
               i.name as item_name, si.quantity, i.unit
        FROM sales_docs d
        JOIN clients c ON d.client_id = c.id
        JOIN sales_items si ON si.doc_id = d.id
        JOIN items i ON si.item_id = i.id
        WHERE d.organization = %s
        ORDER BY d.date DESC, d.id DESC
    '''
    cursor.execute(query, (current_org,))
    rows = cursor.fetchall()
    
    cursor.close()
    conn.close()

    data = []
    for r in rows:
        dt_str = format_date_ru(r['date'])
        qty_str = f"{float(r['quantity']):.2f} {r['unit']}"
        
        data.append({
            'Номер накладной': r['doc_number'],
            'Дата': dt_str,
            'МТТ': r['client_name'],
            'Номенклатура': r['item_name'],
            'Кол-во': qty_str
        })

    df = pd.DataFrame(data)
    if df.empty:
        df = pd.DataFrame(columns=['Номер накладной', 'Дата', 'МТТ', 'Номенклатура', 'Кол-во'])

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Отгрузки')
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f'Отгрузки_{current_org}.xlsx'
    )

@app.route('/sales/delete/<int:id>', methods=['POST'])
def delete_sale(id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM sales_docs WHERE id = %s", (id,))
    conn.commit()
    cursor.close()
    conn.close()
    flash('Накладная отгрузки удалена', 'info')
    return redirect(url_for('sales'))

@app.route('/directory')
def directory():
    current_org = session.get('current_org')
    if not current_org:
        return redirect(url_for('index'))

    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM items WHERE organization = %s ORDER BY name", (current_org,))
    items = cursor.fetchall()

    cursor.execute('''
        SELECT DISTINCT ON (item_id) item_id, price, effective_date 
        FROM item_prices 
        ORDER BY item_id, effective_date DESC, id DESC
    ''')
    prices_raw = cursor.fetchall()
    prices_map = {p['item_id']: {'price': float(p['price']), 'date': p['effective_date']} for p in prices_raw}

    cursor.execute("SELECT * FROM clients ORDER BY category, client_number")
    clients = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template('directory.html', items=items, prices_map=prices_map, clients=clients, current_org=current_org)

@app.route('/directory/add_item', methods=['POST'])
def add_item():
    current_org = session.get('current_org', 'OTABEK UMAR FAYZ')
    name = request.form['name'].strip()
    unit = request.form['unit'].strip()
    initial_price = float(request.form.get('price', 0))
    effective_date = request.form.get('effective_date')

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO items (name, unit, organization) VALUES (%s, %s, %s) RETURNING id",
            (name, unit, current_org)
        )
        row = cursor.fetchone()
        item_id = row['id'] if isinstance(row, dict) else row[0]

        if effective_date and initial_price >= 0:
            cursor.execute(
                "INSERT INTO item_prices (item_id, price, effective_date) VALUES (%s, %s, %s)",
                (item_id, initial_price, effective_date)
            )

        conn.commit()
        flash('Номенклатура и цена успешно добавлены', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Ошибка: {e}', 'danger')
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for('directory'))

@app.route('/directory/update_price/<int:item_id>', methods=['POST'])
def update_price(item_id):
    price = float(request.form.get('price', 0))
    effective_date = request.form.get('effective_date')

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO item_prices (item_id, price, effective_date) VALUES (%s, %s, %s)",
            (item_id, price, effective_date)
        )
        conn.commit()
        flash('Цена успешно обновлена с указанной даты!', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Ошибка при обновлении цены: {e}', 'danger')
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for('directory'))

@app.route('/directory/delete_item/<int:id>', methods=['POST'])
def delete_item(id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM items WHERE id = %s", (id,))
    conn.commit()
    cursor.close()
    conn.close()
    flash('Номенклатура удалена', 'info')
    return redirect(url_for('directory'))

@app.route('/directory/add_client', methods=['POST'])
def add_client():
    category = request.form['category'].strip()
    client_number = int(request.form['client_number'])
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO clients (category, client_number) VALUES (%s, %s)", (category, client_number))
        conn.commit()
        flash('МТТ успешно добавлен', 'success')
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        flash(f'МТТ №{client_number} для категории "{category}" уже существует!', 'warning')
    except Exception as e:
        conn.rollback()
        flash(f'Ошибка: {e}', 'danger')
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for('directory'))

@app.route('/directory/delete_client/<int:id>', methods=['POST'])
def delete_client(id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM clients WHERE id = %s", (id,))
    conn.commit()
    cursor.close()
    conn.close()
    flash('МТТ удален', 'info')
    return redirect(url_for('directory'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
