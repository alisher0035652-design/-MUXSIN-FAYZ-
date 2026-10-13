import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'warehouse_super_secret_key_1c')

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
                name VARCHAR(255) NOT NULL UNIQUE,
                unit VARCHAR(50) NOT NULL
            );
        ''')
        # Таблица цен номенклатуры с привязкой к дате (историчность цен)
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
            CREATE TABLE IF NOT EXISTS purchases (
                id SERIAL PRIMARY KEY,
                date DATE NOT NULL,
                item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
                quantity NUMERIC(12, 2) NOT NULL
            );
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sales_docs (
                id SERIAL PRIMARY KEY,
                doc_number VARCHAR(50) NOT NULL UNIQUE,
                date DATE NOT NULL,
                client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE
            );
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
    """Возвращает актуальную цену товара на указанную дату документа"""
    cursor.execute('''
        SELECT price FROM item_prices 
        WHERE item_id = %s AND effective_date <= %s 
        ORDER BY effective_date DESC, id DESC LIMIT 1
    ''', (item_id, doc_date))
    row = cursor.fetchone()
    if row:
        return float(row['price'] if isinstance(row, dict) else row[0])
    
    # Если точной цены на дату нет, берем самую раннюю или 0
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
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM items ORDER BY name")
    items = cursor.fetchall()
    
    cursor.execute("SELECT item_id, SUM(quantity) as total_in FROM purchases GROUP BY item_id")
    purchases = cursor.fetchall()
    
    cursor.execute("SELECT item_id, SUM(quantity) as total_out FROM sales_items GROUP BY item_id")
    sales = cursor.fetchall()
    
    cursor.close()
    conn.close()

    in_map = {p['item_id']: float(p['total_in']) for p in purchases}
    out_map = {s['item_id']: float(s['total_out']) for s in sales}

    stock_data = []
    for item in items:
        t_in = in_map.get(item['id'], 0.0)
        t_out = out_map.get(item['id'], 0.0)
        stock_data.append({
            'id': item['id'],
            'name': item['name'],
            'unit': item['unit'],
            'total_in': t_in,
            'total_out': t_out,
            'balance': t_in - t_out
        })

    return render_template('index.html', stock_data=stock_data)

@app.route('/sales', methods=['GET', 'POST'])
def sales():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if request.method == 'POST':
        try:
            date = request.form.get('date')
            client_id_raw = request.form.get('client_id')
            
            if not date or not client_id_raw:
                flash('Заполните дату и выберите клиента!', 'danger')
                return redirect(url_for('sales'))

            client_id = int(client_id_raw)
            item_ids = request.form.getlist('item_id[]')
            quantities = request.form.getlist('quantity[]')

            if not item_ids or len(item_ids) == 0:
                flash('Добавьте хотя бы одну позицию товара!', 'warning')
                return redirect(url_for('sales'))

            doc_number = generate_next_doc_number(cursor)

            cursor.execute(
                "INSERT INTO sales_docs (doc_number, date, client_id) VALUES (%s, %s, %s) RETURNING id",
                (doc_number, date, client_id)
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
        SELECT d.id, d.doc_number, d.date, c.category, c.client_number,
               COALESCE((SELECT STRING_AGG(i.name || ' (' || TO_CHAR(si.quantity, 'FM999999999.00') || ' ' || i.unit || ')', ', ') 
                FROM sales_items si JOIN items i ON si.item_id = i.id WHERE si.doc_id = d.id), '') as items_summary
        FROM sales_docs d
        JOIN clients c ON d.client_id = c.id
        WHERE 1=1
    '''
    params = []

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

    cursor.execute("SELECT * FROM items ORDER BY name")
    items = cursor.fetchall()

    cursor.execute("SELECT * FROM clients ORDER BY category, client_number")
    clients = cursor.fetchall()
    
    cursor.close()
    conn.close()

    return render_template('sales.html', 
                           sales_docs=sales_docs, 
                           items=items, 
                           clients=clients, 
                           filter_date=filter_date, 
                           filter_client=filter_client,
                           filter_doc=filter_doc)

@app.route('/sales/edit/<int:doc_id>', methods=['GET', 'POST'])
def edit_sale(doc_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if request.method == 'POST':
        try:
            date = request.form.get('date')
            client_id = int(request.form.get('client_id'))
            
            item_ids = request.form.getlist('item_id[]')
            quantities = request.form.getlist('quantity[]')

            if not item_ids:
                flash('Добавьте хотя бы одну позицию товара!', 'warning')
                return redirect(url_for('edit_sale', doc_id=doc_id))

            cursor.execute(
                "UPDATE sales_docs SET date = %s, client_id = %s WHERE id = %s",
                (date, client_id, doc_id)
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

    cursor.execute("SELECT * FROM items ORDER BY name")
    items = cursor.fetchall()

    cursor.execute("SELECT * FROM clients ORDER BY category, client_number")
    clients = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template('edit_sale.html', doc=doc, sale_items=sale_items, items=items, clients=clients)

@app.route('/sales/print/<int:doc_id>')
def print_sale_route(doc_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT d.id, d.doc_number, d.date, c.category, c.client_number
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

    for r in raw_items:
        qty = float(r['quantity'])
        price = get_price_on_date(cursor, r['item_id'], doc_date)
        sum_supply = qty * price
        vat = sum_supply * 0.12  # Ставка НДС 12%
        sum_with_vat = sum_supply + vat

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

    return render_template('print_sale.html', doc=doc, items=processed_items, totals=totals)

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

@app.route('/purchases', methods=['GET', 'POST'])
def purchases():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if request.method == 'POST':
        try:
            date = request.form['date']
            item_id = int(request.form['item_id'])
            quantity = float(request.form['quantity'])

            cursor.execute(
                "INSERT INTO purchases (date, item_id, quantity) VALUES (%s, %s, %s)",
                (date, item_id, quantity)
            )
            conn.commit()
            flash('Оприходование зарегистрировано!', 'success')
        except Exception as e:
            conn.rollback()
            flash(f'Ошибка при оприходовании: {e}', 'danger')
        finally:
            cursor.close()
            conn.close()
        return redirect(url_for('purchases'))

    cursor.execute('''
        SELECT p.id, p.date, i.name as item_name, i.unit, p.quantity
        FROM purchases p
        JOIN items i ON p.item_id = i.id
        ORDER BY p.date DESC, p.id DESC
    ''')
    purchases_list = cursor.fetchall()

    cursor.execute("SELECT * FROM items ORDER BY name")
    items = cursor.fetchall()
    
    cursor.close()
    conn.close()

    return render_template('purchases.html', purchases=purchases_list, items=items)

@app.route('/purchases/delete/<int:id>', methods=['POST'])
def delete_purchase(id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM purchases WHERE id = %s", (id,))
    conn.commit()
    cursor.close()
    conn.close()
    flash('Документ оприходования удален', 'info')
    return redirect(url_for('purchases'))

@app.route('/directory')
def directory():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM items ORDER BY name")
    items = cursor.fetchall()

    # Получаем последние цены для каждого товара для отображения в справочнике
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
    return render_template('directory.html', items=items, prices_map=prices_map, clients=clients)

@app.route('/directory/add_item', methods=['POST'])
def add_item():
    name = request.form['name'].strip()
    unit = request.form['unit'].strip()
    initial_price = float(request.form.get('price', 0))
    effective_date = request.form.get('effective_date')

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO items (name, unit) VALUES (%s, %s) RETURNING id", (name, unit))
        row = cursor.fetchone()
        item_id = row['id'] if isinstance(row, dict) else row[0]

        if effective_date and initial_price >= 0:
            cursor.execute(
                "INSERT INTO item_prices (item_id, price, effective_date) VALUES (%s, %s, %s)",
                (item_id, initial_price, effective_date)
            )

        conn.commit()
        flash('Номенклатура и цена успешно добавлены', 'success')
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        flash(f'Товар с наименованием "{name}" уже существует!', 'warning')
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
        flash('Клиент успешно добавлен', 'success')
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        flash(f'Клиент №{client_number} для категории "{category}" уже существует!', 'warning')
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
    flash('Клиент удален', 'info')
    return redirect(url_for('directory'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
