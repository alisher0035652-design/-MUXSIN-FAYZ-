import os
from flask import Flask, render_template, request, redirect, url_for, flash
import sqlite3
import pandas as pd

app = Flask(__name__)
app.secret_key = 'warehouse_secret_key_change_me'

def get_db_connection():
    conn = sqlite3.connect('warehouse.db')
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/')
def index():
    conn = get_db_connection()
    purchases = conn.execute('''
        SELECT item_id, SUM(quantity) as total_purchased 
        FROM purchases GROUP BY item_id
    ''').fetchall()
    
    sales = conn.execute('''
        SELECT item_id, client_id, SUM(quantity) as total_sold 
        FROM sales GROUP BY item_id, client_id
    ''').fetchall()
    
    items = conn.execute('SELECT * FROM items').fetchall()
    clients = conn.execute('SELECT * FROM clients').fetchall()
    
    items_dict = {i['id']: i for i in items}
    purchased_map = {p['item_id']: p['total_purchased'] for p in purchases}
    
    sales_by_item = {}
    for s in sales:
        sales_by_item[s['item_id']] = sales_by_item.get(s['item_id'], 0) + s['total_sold']
        
    stock_data = []
    for item_id, item in items_dict.items():
        total_in = purchased_map.get(item_id, 0)
        total_out = sales_by_item.get(item_id, 0)
        stock_balance = total_in - total_out
        stock_data.append({
            'item_id': item_id,
            'name': item['name'],
            'unit': item['unit'],
            'total_in': total_in,
            'total_out': total_out,
            'balance': stock_balance
        })
        
    conn.close()
    return render_template('index.html', stock_data=stock_data)

@app.route('/sales', methods=['GET', 'POST'])
def sales():
    conn = get_db_connection()
    if request.method == 'POST':
        date = request.form['date']
        client_id = request.form['client_id']
        item_id = request.form['item_id']
        quantity = float(request.form['quantity'])
        
        conn.execute('INSERT INTO sales (date, client_id, item_id, quantity) VALUES (?, ?, ?, ?)',
                     (date, client_id, item_id, quantity))
        conn.commit()
        conn.close()
        flash('Отгрузка успешно добавлена!', 'success')
        return redirect(url_for('sales'))
        
    sales_list = conn.execute('''
        SELECT s.id, s.date, c.district, c.city, i.name as item_name, i.unit, s.quantity
        FROM sales s
        JOIN clients c ON s.client_id = c.id
        JOIN items i ON s.item_id = i.id
        ORDER BY s.date DESC
    ''').fetchall()
    
    items = conn.execute('SELECT * FROM items').fetchall()
    clients = conn.execute('SELECT * FROM clients').fetchall()
    conn.close()
    return render_template('sales.html', sales=sales_list, items=items, clients=clients)

@app.route('/sales/delete/<int:id>', methods=['POST'])
def delete_sale(id):
    conn = get_db_connection()
    conn.execute('DELETE FROM sales WHERE id = ?', (id,))
    conn.commit()
    conn.close()
    flash('Запись об отгрузке удалена', 'info')
    return redirect(url_for('sales'))

@app.route('/purchases', methods=['GET', 'POST'])
def purchases():
    conn = get_db_connection()
    if request.method == 'POST':
        date = request.form['date']
        item_id = request.form['item_id']
        quantity = float(request.form['quantity'])
        
        conn.execute('INSERT INTO purchases (date, item_id, quantity) VALUES (?, ?, ?)',
                     (date, item_id, quantity))
        conn.commit()
        conn.close()
        flash('Оприходование успешно добавлено!', 'success')
        return redirect(url_for('purchases'))
        
    purchases_list = conn.execute('''
        SELECT p.id, p.date, i.name as item_name, i.unit, p.quantity
        FROM purchases p
        JOIN items i ON p.item_id = i.id
        ORDER BY p.date DESC
    ''').fetchall()
    
    items = conn.execute('SELECT * FROM items').fetchall()
    conn.close()
    return render_template('purchases.html', purchases=purchases_list, items=items)

@app.route('/purchases/delete/<int:id>', methods=['POST'])
def delete_purchase(id):
    conn = get_db_connection()
    conn.execute('DELETE FROM purchases WHERE id = ?', (id,))
    conn.commit()
    conn.close()
    flash('Запись об оприходовании удалена', 'info')
    return redirect(url_for('purchases'))

@app.route('/directory', methods=['GET'])
def directory():
    conn = get_db_connection()
    items = conn.execute('SELECT * FROM items').fetchall()
    clients = conn.execute('SELECT * FROM clients').fetchall()
    conn.close()
    return render_template('directory.html', items=items, clients=clients)

@app.route('/directory/add_item', methods=['POST'])
def add_item():
    name = request.form['name']
    unit = request.form['unit']
    conn = get_db_connection()
    conn.execute('INSERT INTO items (name, unit) VALUES (?, ?)', (name, unit))
    conn.commit()
    conn.close()
    flash('Номенклатура успешно добавлена', 'success')
    return redirect(url_for('directory'))

@app.route('/directory/delete_item/<int:id>', methods=['POST'])
def delete_item(id):
    conn = get_db_connection()
    conn.execute('DELETE FROM items WHERE id = ?', (id,))
    conn.commit()
    conn.close()
    flash('Номенклатура удалена', 'info')
    return redirect(url_for('directory'))

@app.route('/directory/add_client', methods=['POST'])
def add_client():
    district = request.form['district']
    city = request.form['city']
    conn = get_db_connection()
    conn.execute('INSERT INTO clients (district, city) VALUES (?, ?)', (district, city))
    conn.commit()
    conn.close()
    flash('Клиент успешно добавлен', 'success')
    return redirect(url_for('directory'))

@app.route('/directory/delete_client/<int:id>', methods=['POST'])
def delete_client(id):
    conn = get_db_connection()
    conn.execute('DELETE FROM clients WHERE id = ?', (id,))
    conn.commit()
    conn.close()
    flash('Клиент удален', 'info')
    return redirect(url_for('directory'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
