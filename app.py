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
        # Локальный fallback для тестов
        import sqlite3
        conn = sqlite3.connect('warehouse.db')
        conn.row_factory = sqlite3.Row
    return conn

def init_db_schema():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Создание таблиц с ограничением UNIQUE на имена и клиентов, чтобы избежать дубликатов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS items (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NOT NULL UNIQUE,
            unit VARCHAR(50) NOT NULL
        );
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS clients (
            id SERIAL PRIMARY KEY,
            district VARCHAR(255) NOT NULL,
            city VARCHAR(255),
            UNIQUE(district, city)
        );
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS purchases (
            id SERIAL PRIMARY KEY,
            date DATE NOT NULL,
            item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
            quantity NUMERIC(12, 3) NOT NULL
        );
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sales (
            id SERIAL PRIMARY KEY,
            date DATE NOT NULL,
            client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
            item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
            quantity NUMERIC(12, 3) NOT NULL
        );
    ''')
    conn.commit()
    cursor.close()
    conn.close()

# Инициализация структуры при старте
init_db_schema()

@app.route('/')
def index():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM items ORDER BY name")
    items = cursor.fetchall()
    
    cursor.execute("SELECT item_id, SUM(quantity) as total_in FROM purchases GROUP BY item_id")
    purchases = cursor.fetchall()
    
    cursor.execute("SELECT item_id, SUM(quantity) as total_out FROM sales GROUP BY item_id")
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
            date = request.form['date']
            client_id = int(request.form['client_id'])
            item_id = int(request.form['item_id'])
            quantity = float(request.form['quantity'])

            cursor.execute(
                "INSERT INTO sales (date, client_id, item_id, quantity) VALUES (%s, %s, %s, %s)",
                (date, client_id, item_id, quantity)
            )
            conn.commit()
            flash('Отгрузка успешно зарегистрирована!', 'success')
        except Exception as e:
            conn.rollback()
            flash(f'Ошибка при добавлении отгрузки: {e}', 'danger')
        finally:
            cursor.close()
            conn.close()
        return redirect(url_for('sales'))

    cursor.execute('''
        SELECT s.id, s.date, c.district, c.city, i.name as item_name, i.unit, s.quantity
        FROM sales s
        JOIN clients c ON s.client_id = c.id
        JOIN items i ON s.item_id = i.id
        ORDER BY s.date DESC, s.id DESC
    ''')
    sales_list = cursor.fetchall()

    cursor.execute("SELECT * FROM items ORDER BY name")
    items = cursor.fetchall()

    cursor.execute("SELECT * FROM clients ORDER BY district, city")
    clients = cursor.fetchall()
    
    cursor.close()
    conn.close()

    return render_template('sales.html', sales=sales_list, items=items, clients=clients)

@app.route('/sales/delete/<int:id>', methods=['POST'])
def delete_sale(id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM sales WHERE id = %s", (id,))
    conn.commit()
    cursor.close()
    conn.close()
    flash('Документ отгрузки удален', 'info')
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
    cursor.execute("SELECT * FROM clients ORDER BY district, city")
    clients = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template('directory.html', items=items, clients=clients)

@app.route('/directory/add_item', methods=['POST'])
def add_item():
    name = request.form['name'].strip()
    unit = request.form['unit'].strip()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO items (name, unit) VALUES (%s, %s)", (name, unit))
        conn.commit()
        flash('Номенклатура успешно добавлена', 'success')
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        flash(f'Товар с наименованием "{name}" уже существует в справочнике!', 'warning')
    except Exception as e:
        conn.rollback()
        flash(f'Ошибка: {e}', 'danger')
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
    district = request.form['district'].strip()
    city = request.form['city'].strip() if request.form.get('city') else ''
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO clients (district, city) VALUES (%s, %s)", (district, city))
        conn.commit()
        flash('Клиент успешно добавлен', 'success')
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        flash('Такой клиент (район и город) уже есть в базе!', 'warning')
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
