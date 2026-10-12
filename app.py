import os
from flask import Flask, render_template, request, redirect, url_for, flash
from sqlalchemy import create_engine, text

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'warehouse_super_secret_key_1c')

# Подключение к PostgreSQL или SQLite (fallback)
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///warehouse.db')
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

def init_db_schema():
    with engine.begin() as conn:
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS items (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                unit VARCHAR(50) NOT NULL
            );
        '''))
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS clients (
                id SERIAL PRIMARY KEY,
                district VARCHAR(255) NOT NULL,
                city VARCHAR(255)
            );
        '''))
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS purchases (
                id SERIAL PRIMARY KEY,
                date DATE NOT NULL,
                item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
                quantity NUMERIC(12, 3) NOT NULL
            );
        '''))
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS sales (
                id SERIAL PRIMARY KEY,
                date DATE NOT NULL,
                client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
                item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
                quantity NUMERIC(12, 3) NOT NULL
            );
        '''))

# Инициализируем структуры таблиц при запуске
init_db_schema()

@app.route('/')
def index():
    with engine.connect() as conn:
        items = conn.execute(text("SELECT * FROM items ORDER BY name")).mappings().fetchall()
        
        purchases = conn.execute(text("""
            SELECT item_id, SUM(quantity) as total_in 
            FROM purchases GROUP BY item_id
        """)).mappings().fetchall()
        
        sales = conn.execute(text("""
            SELECT item_id, SUM(quantity) as total_out 
            FROM sales GROUP BY item_id
        """)).mappings().fetchall()

        in_map = {p['item_id']: float(p['total_in']) for p in purchases}
        out_map = {s['item_id']: float(s['total_out']) for s in sales}

        stock_data = []
        for item in items:
            t_in = in_map.get(item['id'], 0.0)
            t_out = out_map.get(item['id'], 0.0)
            balance = t_in - t_out
            stock_data.append({
                'id': item['id'],
                'name': item['name'],
                'unit': item['unit'],
                'total_in': t_in,
                'total_out': t_out,
                'balance': balance
            })

    return render_template('index.html', stock_data=stock_data)

@app.route('/sales', methods=['GET', 'POST'])
def sales():
    with engine.connect() as conn:
        if request.method == 'POST':
            date = request.form['date']
            client_id = int(request.form['client_id'])
            item_id = int(request.form['item_id'])
            quantity = float(request.form['quantity'])

            with engine.begin() as trans_conn:
                trans_conn.execute(
                    text("INSERT INTO sales (date, client_id, item_id, quantity) VALUES (:d, :c, :i, :q)"),
                    {"d": date, "c": client_id, "i": item_id, "q": quantity}
                )
            flash('Отгрузка успешно зарегистрирована в 1С-Склад!', 'success')
            return redirect(url_for('sales'))

        sales_list = conn.execute(text("""
            SELECT s.id, s.date, c.district, c.city, i.name as item_name, i.unit, s.quantity
            FROM sales s
            JOIN clients c ON s.client_id = c.id
            JOIN items i ON s.item_id = i.id
            ORDER BY s.date DESC, s.id DESC
        """)).mappings().fetchall()

        items = conn.execute(text("SELECT * FROM items ORDER BY name")).mappings().fetchall()
        clients = conn.execute(text("SELECT * FROM clients ORDER BY district, city")).mappings().fetchall()

    return render_template('sales.html', sales=sales_list, items=items, clients=clients)

@app.route('/sales/delete/<int:id>', methods=['POST'])
def delete_sale(id):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM sales WHERE id = :id"), {"id": id})
    flash('Документ отгрузки распроведен/удален', 'info')
    return redirect(url_for('sales'))

@app.route('/purchases', methods=['GET', 'POST'])
def purchases():
    with engine.connect() as conn:
        if request.method == 'POST':
            date = request.form['date']
            item_id = int(request.form['item_id'])
            quantity = float(request.form['quantity'])

            with engine.begin() as trans_conn:
                trans_conn.execute(
                    text("INSERT INTO purchases (date, item_id, quantity) VALUES (:d, :i, :q)"),
                    {"d": date, "i": item_id, "q": quantity}
                )
            flash('Оприходование зарегистрировано на складе!', 'success')
            return redirect(url_for('purchases'))

        purchases_list = conn.execute(text("""
            SELECT p.id, p.date, i.name as item_name, i.unit, p.quantity
            FROM purchases p
            JOIN items i ON p.item_id = i.id
            ORDER BY p.date DESC, p.id DESC
        """)).mappings().fetchall()

        items = conn.execute(text("SELECT * FROM items ORDER BY name")).mappings().fetchall()

    return render_template('purchases.html', purchases=purchases_list, items=items)

@app.route('/purchases/delete/<int:id>', methods=['POST'])
def delete_purchase(id):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM purchases WHERE id = :id"), {"id": id})
    flash('Документ оприходования удален', 'info')
    return redirect(url_for('purchases'))

@app.route('/directory')
def directory():
    with engine.connect() as conn:
        items = conn.execute(text("SELECT * FROM items ORDER BY name")).mappings().fetchall()
        clients = conn.execute(text("SELECT * FROM clients ORDER BY district, city")).mappings().fetchall()
    return render_template('directory.html', items=items, clients=clients)

@app.route('/directory/add_item', methods=['POST'])
def add_item():
    name = request.form['name'].strip()
    unit = request.form['unit'].strip()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO items (name, unit) VALUES (:n, :u)"), {"n": name, "u": unit})
    flash('Номенклатура создана', 'success')
    return redirect(url_for('directory'))

@app.route('/directory/delete_item/<int:id>', methods=['POST'])
def delete_item(id):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM items WHERE id = :id"), {"id": id})
    flash('Номенклатура удалена из справочника', 'info')
    return redirect(url_for('directory'))

@app.route('/directory/add_client', methods=['POST'])
def add_client():
    district = request.form['district'].strip()
    city = request.form['city'].strip()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO clients (district, city) VALUES (:d, :c)"), {"d": district, "c": city})
    flash('Контрагент (клиент) добавлен в базу', 'success')
    return redirect(url_for('directory'))

@app.route('/directory/delete_client/<int:id>', methods=['POST'])
def delete_client(id):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM clients WHERE id = :id"), {"id": id})
    flash('Клиент удален из справочника', 'info')
    return redirect(url_for('directory'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
