from datetime import date as date_cls
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
import io
import os
import re
import uuid
import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
import psycopg2
import psycopg2.extras
from flask import (
    Flask,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    send_from_directory,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get(
    'SECRET_KEY', 'warehouse_super_secret_key_1c'
)

ORGANIZATION_NAME = 'MUXSIN-FAYZ'
ORGANIZATIONS = {
    'MUXSIN-FAYZ': {
        'name': 'MUXSIN-FAYZ',
        'address': 'Бекобод 13-даха Фаргона МФЙ',
        'account': '20208000005351182001',
        'bank': 'Миллий Банк. Бекобод БХМ',
        'mfo': '00450',
        'inn': '306242444',
        'has_vat': False,
    },
}


# Блокировка редактирования накладных.
# Накладные, дата которых <= границы, закрыты (кроме админа и открытых админом).
# Пример: сегодня 23/11, EDIT_LOCK_DAYS = 2 -> закрыто до 21/11 включительно.
EDIT_LOCK_TZ = ZoneInfo('Asia/Tashkent')
EDIT_LOCK_DAYS = 2
EDIT_LOCK_WORKING_DAYS = False  # True = считать рабочие дни (Сб/Вс пропускаются)
SALE_LOCKED_MSG = (
    'Накладная закрыта для редактирования. '
    'Изменить её может только администратор.'
)


@app.template_filter('money')
def format_money(value):
  try:
    val = float(value)
    return f'{val:,.2f}'.replace(',', ' ')
  except (ValueError, TypeError):
    return '0.00'


@app.template_filter('date_ru')
def format_date_ru(value):
  if not value:
    return ''
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
  if database_url and database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)

  if database_url:
    conn = psycopg2.connect(
        database_url, cursor_factory=psycopg2.extras.RealDictCursor
    )
  else:
    import sqlite3

    conn = sqlite3.connect('warehouse.db')
    conn.row_factory = sqlite3.Row
  return conn


def next_barcode(cursor, organization):
  cursor.execute('SELECT pg_advisory_xact_lock(727002)')
  cursor.execute(
      "SELECT COALESCE(MAX(SUBSTRING(barcode FROM 5)::int), 0) AS mx"
      " FROM items WHERE organization = %s AND barcode ~ '^SKU-[0-9]+$'",
      (organization,),
  )
  row = cursor.fetchone()
  mx = row['mx'] if isinstance(row, dict) else row[0]
  return f'SKU-{int(mx) + 1:06d}'


def init_db_schema():
  conn = get_db_connection()
  cursor = conn.cursor()
  try:
    cursor.execute('SELECT pg_advisory_xact_lock(727001)')

    cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                full_name VARCHAR(150),
                role VARCHAR(50) DEFAULT 'manager',
                clear_password VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

    cursor.execute('''
            CREATE TABLE IF NOT EXISTS items (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                unit VARCHAR(50) NOT NULL,
                organization VARCHAR(100) DEFAULT 'MUXSIN-FAYZ'
            );
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
                organization VARCHAR(100) DEFAULT 'MUXSIN-FAYZ'
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
    cursor.execute('''
            CREATE TABLE IF NOT EXISTS contracts (
                id SERIAL PRIMARY KEY,
                contract_number VARCHAR(100) NOT NULL,
                contract_date DATE NOT NULL,
                organization VARCHAR(100) DEFAULT 'MUXSIN-FAYZ',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

    cursor.execute(
        'ALTER TABLE items ADD COLUMN IF NOT EXISTS barcode VARCHAR(50)'
    )
    cursor.execute(
        'ALTER TABLE sales_docs ADD COLUMN IF NOT EXISTS submit_token VARCHAR(64)'
    )
    cursor.execute(
        'ALTER TABLE sales_docs ADD COLUMN IF NOT EXISTS status VARCHAR(50)'
    )
    cursor.execute(
        'ALTER TABLE sales_docs ADD COLUMN IF NOT EXISTS user_id INTEGER'
    )
    cursor.execute(
        'ALTER TABLE sales_docs ADD COLUMN IF NOT EXISTS updated_by INTEGER'
    )
    cursor.execute(
        'ALTER TABLE sales_docs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP'
    )
    cursor.execute(
        'ALTER TABLE sales_docs ADD COLUMN IF NOT EXISTS edit_unlocked BOOLEAN DEFAULT FALSE'
    )
    cursor.execute(
        'ALTER TABLE sales_items ADD COLUMN IF NOT EXISTS price NUMERIC(12, 2)'
    )
    cursor.execute(
        'ALTER TABLE sales_items ADD COLUMN IF NOT EXISTS sum NUMERIC(12, 2)'
    )
    cursor.execute(
        'ALTER TABLE users ADD COLUMN IF NOT EXISTS clear_password VARCHAR(100)'
    )

    cursor.execute('SELECT COUNT(*) AS cnt FROM users')
    if cursor.fetchone()['cnt'] == 0:
      default_pass = generate_password_hash('admin123')
      cursor.execute(
          'INSERT INTO users (username, password_hash, full_name, role,'
          ' clear_password) VALUES (%s, %s, %s, %s, %s)',
          ('admin', default_pass, 'Главный Администратор', 'admin', 'admin123'),
      )

    conn.commit()
  except Exception as e:
    conn.rollback()
    print(f'Schema init error: {e}')
  finally:
    cursor.close()
    conn.close()


init_db_schema()


def today_local():
  return datetime.now(EDIT_LOCK_TZ).date()


def lock_boundary(today):
  """Последняя дата (включительно), которая уже закрыта для редактирования."""
  if not EDIT_LOCK_WORKING_DAYS:
    return today - timedelta(days=EDIT_LOCK_DAYS)
  result, count = today, 0
  while count < EDIT_LOCK_DAYS:
    result -= timedelta(days=1)
    if result.weekday() < 5:
      count += 1
  return result


def is_admin():
  return session.get('user_role') == 'admin'


def can_edit_sale(doc_date, edit_unlocked=False):
  if is_admin() or edit_unlocked:
    return True
  if isinstance(doc_date, str):
    doc_date = datetime.strptime(doc_date[:10], '%Y-%m-%d').date()
  return doc_date > lock_boundary(today_local())


@app.before_request
def check_auth():
  session['current_org'] = ORGANIZATION_NAME
  allowed_endpoints = ['login', 'static', 'favicon', 'apple_touch_icon', 'create_draft_sale_from_raznaryadka']
  if request.endpoint not in allowed_endpoints and 'user_id' not in session:
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
  if 'user_id' in session:
    return redirect(url_for('sales'))

  if request.method == 'POST':
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
      cursor.execute(
          'SELECT id, username, password_hash, full_name, role FROM users WHERE'
          ' username = %s',
          (username,),
      )
      user = cursor.fetchone()
    except Exception:
      user = None
    finally:
      cursor.close()
      conn.close()

    if user and check_password_hash(user['password_hash'], password):
      session['user_id'] = user['id']
      session['username'] = user['username']
      session['user_full_name'] = user['full_name']
      session['user_role'] = user['role']
      flash('Успешный вход в систему!', 'success')
      return redirect(url_for('sales'))
    else:
      flash('Неверный логин или пароль!', 'danger')

  return render_template('login.html')


@app.route('/logout')
def logout():
  session.clear()
  flash('Вы вышли из системы', 'info')
  return redirect(url_for('login'))


@app.after_request
def add_favicon_link(response):
  """Добавляет ссылку на иконку в <head> всех HTML-страниц (шаблоны не меняются)."""
  if response.mimetype != 'text/html' or response.direct_passthrough:
    return response
  body = response.get_data(as_text=True)
  if '</head>' in body and 'rel="icon"' not in body:
    links = (
        '<link rel="icon" type="image/png" sizes="192x192" href="%s">\n'
        '<link rel="apple-touch-icon" href="%s">\n'
    ) % (url_for('static', filename='icon-192.png'),
         url_for('apple_touch_icon'))
    response.set_data(body.replace('</head>', links + '</head>', 1))
  return response


@app.route('/favicon.ico')
def favicon():
  return send_from_directory(
      os.path.join(app.root_path, 'static'),
      'favicon.ico',
      mimetype='image/x-icon',
  )


@app.route('/apple-touch-icon.png')
def apple_touch_icon():
  return send_from_directory(
      os.path.join(app.root_path, 'static'),
      'apple-touch-icon.png',
      mimetype='image/png',
  )


@app.route('/')
def index():
  if 'user_id' not in session:
    return redirect(url_for('login'))
  return redirect(url_for('sales'))


@app.route('/sales', methods=['GET', 'POST'])
def sales():
  # Обычный пользователь по умолчанию видит только свои накладные.
  # Если параметр фильтра сотрудника не указан в адресе, подставляем его.
  # Админ по умолчанию видит накладные всех сотрудников.
  if (
      request.method == 'GET'
      and not is_admin()
      and 'filter_employee' not in request.args
  ):
    args = request.args.to_dict()
    args['filter_employee'] = str(session.get('user_id'))
    return redirect(url_for('sales', **args))

  current_org = ORGANIZATION_NAME
  conn = get_db_connection()
  cursor = conn.cursor()

  if request.method == 'POST':
    try:
      date = request.form.get('date', '').strip()
      client_id_raw = request.form.get('client_id')
      token = request.form.get('submit_token', '').strip()[:64] or None
      user_id = session.get('user_id')

      if not date or not client_id_raw:
        raise ValueError('Заполните дату и выберите МТТ!')
      try:
        parsed_date = datetime.strptime(date, '%Y-%m-%d').date()
        client_id = int(client_id_raw)
      except ValueError:
        raise ValueError('Некорректная дата или МТТ!')
      if not can_edit_sale(parsed_date):
        raise ValueError(
            'Нельзя провести накладную с датой, закрытой для редактирования!'
        )

      cursor.execute('SELECT 1 FROM clients WHERE id = %s', (client_id,))
      if not cursor.fetchone():
        raise ValueError('Выбранный МТТ не найден!')

      item_ids = request.form.getlist('item_id[]')
      quantities = request.form.getlist('quantity[]')

      cursor.execute(
          'SELECT id FROM items WHERE organization = %s', (current_org,)
      )
      allowed_ids = {r['id'] for r in cursor.fetchall()}

      merged = {}
      for idx, raw_item in enumerate(item_ids):
        raw_item = (raw_item or '').strip()
        raw_qty = quantities[idx] if idx < len(quantities) else ''
        raw_qty = (raw_qty or '').strip().replace(',', '.')
        if not raw_item and not raw_qty:
          continue
        if not raw_item or not raw_qty:
          raise ValueError('В каждой позиции укажите товар и количество!')
        try:
          item_id = int(raw_item)
          qty = Decimal(raw_qty)
        except (ValueError, InvalidOperation):
          raise ValueError('Некорректный товар или количество!')
        if not qty.is_finite() or qty <= 0:
          raise ValueError('Количество должно быть больше нуля!')
        if qty != qty.quantize(Decimal('0.01')):
          raise ValueError('Количество: не более 2 знаков после запятой!')
        if item_id not in allowed_ids:
          raise ValueError('Товар не найден в организации!')
        merged[item_id] = merged.get(item_id, Decimal('0')) + qty

      if not merged:
        raise ValueError('Добавьте хотя бы одну позицию товара!')

      cursor.execute('SELECT pg_advisory_xact_lock(727003)')
      if token:
        cursor.execute(
            'SELECT doc_number FROM sales_docs WHERE submit_token = %s', (token,)
        )
        dup = cursor.fetchone()
        if dup:
          conn.rollback()
          flash(f'Накладная № {dup["doc_number"]} уже была сохранена.', 'info')
          return redirect(url_for('sales'))

      cursor.execute(
          'SELECT doc_number FROM sales_docs ORDER BY id DESC LIMIT 1'
      )
      last_doc = cursor.fetchone()
      if not last_doc:
        doc_number = '001001'
      else:
        last_num = ''.join(
            filter(str.isdigit, str(last_doc['doc_number']))
        )
        doc_number = (
            f'{int(last_num) + 1:06d}' if last_num else '001001'
        )

      cursor.execute(
          'INSERT INTO sales_docs (doc_number, date, client_id, organization,'
          ' submit_token, user_id) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id',
          (doc_number, date, client_id, current_org, token, user_id),
      )
      row = cursor.fetchone()
      doc_id = row['id'] if isinstance(row, dict) else row[0]

      for item_id, qty in merged.items():
        cursor.execute(
            'INSERT INTO sales_items (doc_id, item_id, quantity) VALUES'
            ' (%s, %s, %s)',
            (doc_id, item_id, qty),
        )

      conn.commit()
      flash(f'Накладная № {doc_number} успешно проведена!', 'success')
    except ValueError as e:
      conn.rollback()
      flash(str(e), 'danger')
    except Exception as e:
      conn.rollback()
      flash(f'Ошибка при создании отгрузки: {e}', 'danger')
    finally:
      cursor.close()
      conn.close()
    return redirect(url_for('sales'))

  filter_date_from = request.args.get('filter_date_from', '').strip()
  filter_date_to = request.args.get('filter_date_to', '').strip()
  filter_client = request.args.get('filter_client', '').strip()
  filter_doc = request.args.get('filter_doc', '').strip()
  filter_employee = request.args.get('filter_employee', '').strip()
  filter_item = request.args.get('filter_item', '').strip()

  query = """
        SELECT d.id, d.doc_number, d.date, c.category, c.client_number, d.organization,
               COALESCE(u.full_name, 'Система') as author_name,
               COALESCE(uu.full_name, '') as editor_name,
               d.updated_at, d.edit_unlocked,
               COALESCE((SELECT STRING_AGG(i.name || ' (' || TO_CHAR(si.quantity, 'FM999999999.00') || ' ' || i.unit || ')', ', ') 
                FROM sales_items si JOIN items i ON si.item_id = i.id WHERE si.doc_id = d.id), '') as items_summary
        FROM sales_docs d
        JOIN clients c ON d.client_id = c.id
        LEFT JOIN users u ON d.user_id = u.id
        LEFT JOIN users uu ON d.updated_by = uu.id
        WHERE d.organization = %s
    """
  params = [current_org]

  if filter_date_from:
    query += ' AND d.date >= %s'
    params.append(filter_date_from)
  if filter_date_to:
    query += ' AND d.date <= %s'
    params.append(filter_date_to)
  if filter_client:
    query += ' AND d.client_id = %s'
    params.append(int(filter_client))
  if filter_doc:
    query += ' AND d.doc_number ILIKE %s'
    params.append(f'%{filter_doc}%')
  if filter_employee == 'system':
    query += ' AND d.user_id IS NULL'
  elif filter_employee.isdigit():
    query += ' AND d.user_id = %s'
    params.append(int(filter_employee))
  if filter_item.isdigit():
    query += (' AND EXISTS (SELECT 1 FROM sales_items fsi'
              ' WHERE fsi.doc_id = d.id AND fsi.item_id = %s)')
    params.append(int(filter_item))

  query += ' ORDER BY CAST(d.doc_number AS INTEGER) DESC, d.id DESC'

  cursor.execute(query, params)
  sales_docs = cursor.fetchall()
  editable_ids = {
      d['id'] for d in sales_docs
      if can_edit_sale(d['date'], d['edit_unlocked'])
  }

  cursor.execute(
      'SELECT * FROM items WHERE organization = %s ORDER BY name', (current_org,)
  )
  items = cursor.fetchall()

  cursor.execute('SELECT * FROM clients ORDER BY category, client_number')
  clients = cursor.fetchall()

  cursor.execute(
      'SELECT * FROM contracts WHERE organization = %s ORDER BY id DESC',
      (current_org,),
  )
  contracts = cursor.fetchall()

  cursor.execute('SELECT id, full_name, username FROM users ORDER BY full_name')
  users_list = cursor.fetchall()

  cursor.close()
  conn.close()

  return render_template(
      'sales.html',
      sales_docs=sales_docs,
      editable_ids=editable_ids,
      is_admin=is_admin(),
      edit_open_from=lock_boundary(today_local()) + timedelta(days=1),
      items=items,
      clients=clients,
      contracts=contracts,
      users_list=users_list,
      current_org=current_org,
      filter_date_from=filter_date_from,
      filter_date_to=filter_date_to,
      filter_client=filter_client,
      filter_doc=filter_doc,
      filter_employee=filter_employee,
      filter_item=filter_item,
      submit_token=uuid.uuid4().hex,
  )


@app.route('/sales/edit/<int:doc_id>', methods=['GET', 'POST'])
def edit_sale(doc_id):
  current_org = ORGANIZATION_NAME
  conn = get_db_connection()
  cursor = conn.cursor()

  cursor.execute(
      'SELECT id, date, edit_unlocked FROM sales_docs WHERE id = %s', (doc_id,)
  )
  doc_lock = cursor.fetchone()
  if not doc_lock:
    cursor.close()
    conn.close()
    flash('Накладная не найдена', 'danger')
    return redirect(url_for('sales'))
  unlocked = bool(doc_lock['edit_unlocked'])
  if not can_edit_sale(doc_lock['date'], unlocked):
    cursor.close()
    conn.close()
    flash(SALE_LOCKED_MSG, 'danger')
    return redirect(url_for('sales'))

  if request.method == 'POST':
    try:
      date = request.form.get('date')
      client_id = int(request.form.get('client_id'))
      new_date = datetime.strptime(date, '%Y-%m-%d').date() if date else None
      if not new_date or not can_edit_sale(new_date, unlocked):
        raise ValueError('Нельзя указать дату, закрытую для редактирования!')
      item_ids = request.form.getlist('item_id[]')
      quantities = request.form.getlist('quantity[]')
      user_id = session.get('user_id')

      cursor.execute(
          'UPDATE sales_docs SET date = %s, client_id = %s, organization = %s,'
          ' updated_by = %s, updated_at = NOW() WHERE id = %s',
          (date, client_id, current_org, user_id, doc_id),
      )
      cursor.execute('DELETE FROM sales_items WHERE doc_id = %s', (doc_id,))

      for i in range(len(item_ids)):
        if i < len(quantities) and quantities[i]:
          qty_str = str(quantities[i]).strip()
          if qty_str:
            cursor.execute(
                'INSERT INTO sales_items (doc_id, item_id, quantity) VALUES'
                ' (%s, %s, %s)',
                (doc_id, int(item_ids[i]), float(qty_str)),
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

  cursor.execute('SELECT * FROM sales_docs WHERE id = %s', (doc_id,))
  doc = cursor.fetchone()
  if not doc:
    cursor.close()
    conn.close()
    flash('Накладная не найдена', 'danger')
    return redirect(url_for('sales'))

  cursor.execute('SELECT * FROM sales_items WHERE doc_id = %s', (doc_id,))
  sale_items = cursor.fetchall()
  cursor.execute(
      'SELECT * FROM items WHERE organization = %s ORDER BY name', (current_org,)
  )
  items = cursor.fetchall()
  cursor.execute('SELECT * FROM clients ORDER BY category, client_number')
  clients = cursor.fetchall()
  cursor.close()
  conn.close()

  return render_template(
      'edit_sale.html',
      doc=doc,
      sale_items=sale_items,
      items=items,
      clients=clients,
      current_org=current_org,
  )


@app.route('/sales/print/<int:doc_id>')
def print_sale_route(doc_id):
  show_prices = request.args.get('show_prices') != '0'
  contract_id = request.args.get('contract_id', '')

  conn = get_db_connection()
  cursor = conn.cursor()
  cursor.execute(
      """
        SELECT d.id, d.doc_number, d.date, c.category, c.client_number, d.organization
        FROM sales_docs d JOIN clients c ON d.client_id = c.id WHERE d.id = %s
    """,
      (doc_id,),
  )
  doc = cursor.fetchone()
  if not doc:
    cursor.close()
    conn.close()
    flash('Накладная не найдена', 'danger')
    return redirect(url_for('sales'))

  selected_contract = None
  if contract_id:
    cursor.execute('SELECT * FROM contracts WHERE id = %s', (contract_id,))
    selected_contract = cursor.fetchone()

  org_data = ORGANIZATIONS[ORGANIZATION_NAME]

  cursor.execute(
      """
        SELECT i.id as item_id, i.name as item_name, i.unit, si.quantity
        FROM sales_items si JOIN items i ON si.item_id = i.id WHERE si.doc_id = %s
    """,
      (doc_id,),
  )
  raw_items = cursor.fetchall()

  total_sum = 0.0
  total_vat = 0.0
  total_with_vat = 0.0
  processed_items = []
  has_vat = org_data.get('has_vat', False)

  for r in raw_items:
    qty = float(r['quantity'])
    cursor.execute(
        """
            SELECT price FROM item_prices WHERE item_id = %s AND effective_date <= %s ORDER BY effective_date DESC, id DESC LIMIT 1
        """,
        (r['item_id'], doc['date']),
    )
    p_row = cursor.fetchone()
    if not p_row:
      cursor.execute(
          """
                SELECT price FROM item_prices WHERE item_id = %s ORDER BY effective_date ASC, id ASC LIMIT 1
            """,
          (r['item_id'],),
      )
      p_row = cursor.fetchone()

    price = float(p_row['price']) if p_row else 0.0
    sum_supply = qty * price
    vat = sum_supply * 0.12 if has_vat else 0.0
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
        'sum_with_vat': sum_with_vat,
    })

  cursor.close()
  conn.close()
  totals = {
      'sum_supply': total_sum,
      'vat': total_vat,
      'sum_with_vat': total_with_vat,
  }

  return render_template(
      'print_sale.html',
      doc=doc,
      org=org_data,
      items=processed_items,
      totals=totals,
      show_prices=show_prices,
      has_vat=has_vat,
      contract=selected_contract,
  )


@app.route('/directory')
def directory():
  if session.get('user_role') != 'admin':
    flash('Доступ к справочникам разрешен только администратору', 'danger')
    return redirect(url_for('sales'))

  current_org = ORGANIZATION_NAME
  conn = get_db_connection()
  cursor = conn.cursor()
  cursor.execute(
      'SELECT * FROM items WHERE organization = %s ORDER BY name', (current_org,)
  )
  items = cursor.fetchall()

  cursor.execute("""
        SELECT DISTINCT ON (item_id) item_id, price, effective_date 
        FROM item_prices ORDER BY item_id, effective_date DESC, id DESC
    """)
  prices_raw = cursor.fetchall()
  prices_map = {
      p['item_id']: {'price': float(p['price']), 'date': p['effective_date']}
      for p in prices_raw
  }

  cursor.execute('SELECT * FROM clients ORDER BY category, client_number')
  clients = cursor.fetchall()
  cursor.execute(
      'SELECT * FROM contracts WHERE organization = %s ORDER BY id DESC',
      (current_org,),
  )
  contracts = cursor.fetchall()

  try:
    cursor.execute(
        'SELECT id, username, full_name, role, clear_password, created_at FROM'
        ' users ORDER BY id'
    )
    users_list = cursor.fetchall()
  except Exception:
    users_list = []

  cursor.close()
  conn.close()

  return render_template(
      'directory.html',
      items=items,
      prices_map=prices_map,
      clients=clients,
      contracts=contracts,
      users_list=users_list,
      current_org=current_org,
  )


@app.route('/directory/add_user', methods=['POST'])
def add_user():
  if session.get('user_role') != 'admin':
    flash('Доступно только администратору', 'danger')
    return redirect(url_for('sales'))

  username = request.form.get('username', '').strip()
  password = request.form.get('password', '').strip()
  full_name = request.form.get('full_name', '').strip()
  role = request.form.get('role', 'manager')

  if username and password:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
      pwd_hash = generate_password_hash(password)
      cursor.execute(
          'INSERT INTO users (username, password_hash, full_name, role,'
          ' clear_password) VALUES (%s, %s, %s, %s, %s)',
          (username, pwd_hash, full_name, role, password),
      )
      conn.commit()
      flash(f'Сотрудник {username} успешно добавлен!', 'success')
    except psycopg2.errors.UniqueViolation:
      conn.rollback()
      flash('Пользователь с таким логином уже существует!', 'warning')
    except Exception as e:
      conn.rollback()
      flash(f'Ошибка: {e}', 'danger')
    finally:
      cursor.close()
      conn.close()
  return redirect(url_for('directory'))


@app.route('/directory/edit_user/<int:id>', methods=['POST'])
def edit_user(id):
  if session.get('user_role') != 'admin':
    flash('Доступно только администратору', 'danger')
    return redirect(url_for('sales'))

  full_name = request.form.get('full_name', '').strip()
  role = request.form.get('role', 'manager').strip()
  new_password = request.form.get('password', '').strip()

  conn = get_db_connection()
  cursor = conn.cursor()
  try:
    if new_password:
      pwd_hash = generate_password_hash(new_password)
      cursor.execute(
          'UPDATE users SET full_name = %s, role = %s, password_hash = %s,'
          ' clear_password = %s WHERE id = %s',
          (full_name, role, pwd_hash, new_password, id),
      )
    else:
      cursor.execute(
          'UPDATE users SET full_name = %s, role = %s WHERE id = %s',
          (full_name, role, id),
      )
    conn.commit()
    flash('Данные сотрудника успешно обновлены!', 'success')
  except Exception as e:
    conn.rollback()
    flash(f'Ошибка при обновлении сотрудника: {e}', 'danger')
  finally:
    cursor.close()
    conn.close()
  return redirect(url_for('directory'))


@app.route('/directory/delete_user/<int:id>', methods=['POST'])
def delete_user(id):
  if session.get('user_role') != 'admin':
    flash('Доступно только администратору', 'danger')
    return redirect(url_for('sales'))

  if id == session.get('user_id'):
    flash('Нельзя удалить свой собственный аккаунт!', 'warning')
    return redirect(url_for('directory'))

  conn = get_db_connection()
  cursor = conn.cursor()
  cursor.execute('DELETE FROM users WHERE id = %s', (id,))
  conn.commit()
  cursor.close()
  conn.close()
  flash('Сотрудник удален', 'info')
  return redirect(url_for('directory'))


@app.route('/directory/add_contract', methods=['POST'])
def add_contract():
  if session.get('user_role') != 'admin':
    return redirect(url_for('sales'))
  current_org = ORGANIZATION_NAME
  num = request.form.get('contract_number', '').strip()
  dt = request.form.get('contract_date')
  if num and dt:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO contracts (contract_number, contract_date, organization)'
        ' VALUES (%s, %s, %s)',
        (num, dt, current_org),
    )
    conn.commit()
    cursor.close()
    conn.close()
    flash('Договор успешно добавлен!', 'success')
  return redirect(url_for('directory'))


@app.route('/directory/delete_contract/<int:id>', methods=['POST'])
def delete_contract(id):
  if session.get('user_role') != 'admin':
    return redirect(url_for('sales'))
  conn = get_db_connection()
  cursor = conn.cursor()
  cursor.execute('DELETE FROM contracts WHERE id = %s', (id,))
  conn.commit()
  cursor.close()
  conn.close()
  flash('Договор удален', 'info')
  return redirect(url_for('directory'))


@app.route('/sales/export_excel')
def export_sales_excel():
  current_org = ORGANIZATION_NAME

  # Применяем те же фильтры при скачивании, что и на экране
  filter_date_from = request.args.get('filter_date_from', '').strip()
  filter_date_to = request.args.get('filter_date_to', '').strip()
  filter_client = request.args.get('filter_client', '').strip()
  filter_doc = request.args.get('filter_doc', '').strip()
  filter_employee = request.args.get('filter_employee', '').strip()
  filter_item = request.args.get('filter_item', '').strip()

  conn = get_db_connection()
  cursor = conn.cursor()

  query = """
        SELECT d.doc_number, d.date, c.category || ' — МТТ №' || c.client_number as client_name,
               i.name as item_name, si.quantity, i.unit,
               COALESCE(u.full_name, 'Система') as author_name
        FROM sales_docs d 
        JOIN clients c ON d.client_id = c.id
        JOIN sales_items si ON si.doc_id = d.id 
        JOIN items i ON si.item_id = i.id
        LEFT JOIN users u ON d.user_id = u.id
        WHERE d.organization = %s
    """
  params = [current_org]

  if filter_date_from:
    query += ' AND d.date >= %s'
    params.append(filter_date_from)
  if filter_date_to:
    query += ' AND d.date <= %s'
    params.append(filter_date_to)
  if filter_client:
    query += ' AND d.client_id = %s'
    params.append(int(filter_client))
  if filter_doc:
    query += ' AND d.doc_number ILIKE %s'
    params.append(f'%{filter_doc}%')
  if filter_employee == 'system':
    query += ' AND d.user_id IS NULL'
  elif filter_employee.isdigit():
    query += ' AND d.user_id = %s'
    params.append(int(filter_employee))
  if filter_item.isdigit():
    query += (' AND EXISTS (SELECT 1 FROM sales_items fsi'
              ' WHERE fsi.doc_id = d.id AND fsi.item_id = %s)')
    params.append(int(filter_item))

  query += ' ORDER BY CAST(d.doc_number AS INTEGER) DESC, d.id DESC'

  cursor.execute(query, params)
  rows = cursor.fetchall()
  cursor.close()
  conn.close()

  data = []
  for r in rows:
    data.append({
        'Номер накладной': r['doc_number'],
        'Дата': format_date_ru(r['date']),
        'МТТ': r['client_name'],
        'Номенклатура': r['item_name'],
        'Кол-во': float(r['quantity']),
        'Ед. изм.': r['unit'],
        'Сотрудник': r['author_name'],
    })

  df = pd.DataFrame(data)
  if df.empty:
    df = pd.DataFrame(
        columns=[
            'Номер накладной',
            'Дата',
            'МТТ',
            'Номенклатура',
            'Кол-во',
            'Ед. изм.',
            'Сотрудник',
        ]
    )

  output = io.BytesIO()
  with pd.ExcelWriter(output, engine='openpyxl') as writer:
    df.to_excel(writer, index=False, sheet_name='Отгрузки')
  output.seek(0)
  return send_file(
      output,
      mimetype=(
          'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
      ),
      as_attachment=True,
      download_name=f'Отгрузки_{current_org}.xlsx',
  )


@app.route('/sales/delete/<int:id>', methods=['POST'])
def delete_sale(id):
  conn = get_db_connection()
  cursor = conn.cursor()
  cursor.execute(
      'SELECT date, edit_unlocked FROM sales_docs WHERE id = %s', (id,)
  )
  doc_lock = cursor.fetchone()
  if doc_lock and not can_edit_sale(doc_lock['date'], doc_lock['edit_unlocked']):
    cursor.close()
    conn.close()
    flash(SALE_LOCKED_MSG, 'danger')
    return redirect(url_for('sales'))
  cursor.execute('DELETE FROM sales_docs WHERE id = %s', (id,))
  conn.commit()
  cursor.close()
  conn.close()
  flash('Накладная отгрузки удалена', 'info')
  return redirect(url_for('sales'))


@app.route('/sales/toggle_lock/<int:doc_id>', methods=['POST'])
def toggle_sale_lock(doc_id):
  if not is_admin():
    flash('Открывать накладные для редактирования может только администратор.', 'danger')
    return redirect(url_for('sales'))
  conn = get_db_connection()
  cursor = conn.cursor()
  try:
    cursor.execute(
        'UPDATE sales_docs SET edit_unlocked = NOT COALESCE(edit_unlocked, FALSE)'
        ' WHERE id = %s RETURNING edit_unlocked',
        (doc_id,),
    )
    row = cursor.fetchone()
    conn.commit()
    if row is None:
      flash('Накладная не найдена', 'danger')
    elif row['edit_unlocked']:
      flash('Накладная открыта для редактирования.', 'success')
    else:
      flash('Накладная снова закрыта для редактирования.', 'info')
  except Exception as e:
    conn.rollback()
    flash(f'Ошибка: {e}', 'danger')
  finally:
    cursor.close()
    conn.close()
  return redirect(url_for('sales'))


IMPORT_HEADERS = [
    'Дата (ГГГГ-ММ-ДД)',
    'Категория МТТ',
    'Номер МТТ',
    'Наименование товара',
    'Ед. изм.',
    'Количество',
]


def parse_import_date(value):
  if isinstance(value, datetime):
    return value.date()
  if isinstance(value, date_cls):
    return value
  text = str(value or '').strip()
  for fmt in ('%Y-%m-%d', '%d.%m.%Y'):
    try:
      return datetime.strptime(text, fmt).date()
    except ValueError:
      pass
  raise ValueError('дата должна быть в формате ГГГГ-ММ-ДД')


@app.route('/sales/import_template')
def sales_import_template():
  if not is_admin():
    flash('Импорт доступен только администратору.', 'danger')
    return redirect(url_for('sales'))
  wb = Workbook()
  ws = wb.active
  ws.title = 'Накладные'
  ws.append(IMPORT_HEADERS)
  header_fill = PatternFill('solid', fgColor='343A40')
  for cell in ws[1]:
    cell.font = Font(bold=True, color='FFFFFF')
    cell.fill = header_fill
    cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
  for col, width in zip('ABCDEF', [20, 30, 14, 42, 12, 14]):
    ws.column_dimensions[col].width = width
  for row in ws.iter_rows(min_row=2, max_row=1000, min_col=1, max_col=1):
    for cell in row:
      cell.number_format = 'yyyy-mm-dd'
  ws.freeze_panes = 'A2'

  info = wb.create_sheet('Инструкция')
  for line in [
      'Как заполнить:',
      '1. Одна строка — одна позиция товара.',
      '2. Строки с одинаковой датой и одинаковым МТТ попадут в одну накладную.',
      '3. Дата в формате ГГГГ-ММ-ДД, например 2026-11-23.',
      '4. Категория и номер МТТ — как на листе «МТТ».',
      '5. Наименование товара — как на листе «Товары» (ед. изм. можно не указывать,'
      ' если название в справочнике одно).',
      '6. Количество: число, не более 2 знаков после запятой, больше нуля.',
      '7. Если хоть одна строка содержит ошибку, ни одна накладная не сохранится.',
      '8. Даты, закрытые для редактирования, импортировать нельзя.',
  ]:
    info.append([line])
  info.column_dimensions['A'].width = 100

  conn = get_db_connection()
  cursor = conn.cursor()
  try:
    cursor.execute('SELECT category, client_number FROM clients ORDER BY category, client_number')
    mtt_ws = wb.create_sheet('МТТ')
    mtt_ws.append(['Категория МТТ', 'Номер МТТ'])
    for r in cursor.fetchall():
      mtt_ws.append([r['category'], r['client_number']])
    mtt_ws.column_dimensions['A'].width = 40

    cursor.execute(
        'SELECT name, unit FROM items WHERE organization = %s ORDER BY name',
        (ORGANIZATION_NAME,),
    )
    items_ws = wb.create_sheet('Товары')
    items_ws.append(['Наименование товара', 'Ед. изм.'])
    for r in cursor.fetchall():
      items_ws.append([r['name'], r['unit']])
    items_ws.column_dimensions['A'].width = 45
  finally:
    cursor.close()
    conn.close()

  buf = io.BytesIO()
  wb.save(buf)
  buf.seek(0)
  return send_file(
      buf,
      as_attachment=True,
      download_name='шаблон_накладных.xlsx',
      mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  )


@app.route('/sales/import', methods=['POST'])
def sales_import():
  if not is_admin():
    flash('Импорт доступен только администратору.', 'danger')
    return redirect(url_for('sales'))
  upload = request.files.get('excel_file')
  if not upload or not upload.filename:
    flash('Выберите файл Excel для загрузки.', 'danger')
    return redirect(url_for('sales'))
  if not upload.filename.lower().endswith('.xlsx'):
    flash('Нужен файл в формате .xlsx (шаблон из системы).', 'danger')
    return redirect(url_for('sales'))
  try:
    wb = load_workbook(io.BytesIO(upload.read()), data_only=True)
  except Exception:
    flash('Не удалось открыть файл. Проверьте, что это .xlsx.', 'danger')
    return redirect(url_for('sales'))

  ws = wb['Накладные'] if 'Накладные' in wb.sheetnames else wb.worksheets[0]
  rows = list(ws.iter_rows(values_only=True))
  if not rows:
    flash('Файл пустой.', 'danger')
    return redirect(url_for('sales'))
  header = [str(c or '').strip() for c in (list(rows[0]) + [None] * 6)[:6]]
  if header != IMPORT_HEADERS:
    flash('Столбцы не совпадают с шаблоном. Скачайте шаблон и заполните его заново.', 'danger')
    return redirect(url_for('sales'))

  conn = get_db_connection()
  cursor = conn.cursor()
  try:
    cursor.execute('SELECT id, category, client_number FROM clients')
    clients_map = {
        (r['category'].strip().lower(), int(r['client_number'])): r['id']
        for r in cursor.fetchall()
    }
    cursor.execute(
        'SELECT id, name, unit FROM items WHERE organization = %s',
        (ORGANIZATION_NAME,),
    )
    items_map = {}
    for r in cursor.fetchall():
      items_map.setdefault(r['name'].strip().lower(), []).append(r)

    errors = []
    groups = {}  # (дата, client_id) -> {item_id: количество}
    for idx, raw_row in enumerate(rows[1:], start=2):
      row = (list(raw_row) + [None] * 6)[:6]
      if all(v is None or str(v).strip() == '' for v in row):
        continue
      try:
        doc_date = parse_import_date(row[0])
        category = str(row[1] or '').strip()
        try:
          client_number = int(float(str(row[2] or '').strip()))
        except ValueError:
          raise ValueError('номер МТТ должен быть числом')
        client_id = clients_map.get((category.lower(), client_number))
        if not client_id:
          raise ValueError(f'МТТ «{category} №{client_number}» не найден в справочнике')

        item_name = str(row[3] or '').strip()
        unit = str(row[4] or '').strip()
        if not item_name:
          raise ValueError('не указано наименование товара')
        candidates = items_map.get(item_name.lower(), [])
        if unit:
          candidates = [c for c in candidates if c['unit'].strip().lower() == unit.lower()]
        if not candidates:
          raise ValueError(f'товар «{item_name}» не найден в справочнике организации')
        if len(candidates) > 1:
          raise ValueError(f'товар «{item_name}» есть в нескольких ед. изм., укажите ее')
        item_id = candidates[0]['id']

        try:
          qty = Decimal(str(row[5] if row[5] is not None else '').strip().replace(',', '.'))
        except InvalidOperation:
          raise ValueError('количество должно быть числом')
        if not qty.is_finite() or qty <= 0:
          raise ValueError('количество должно быть больше нуля')
        if qty != qty.quantize(Decimal('0.01')):
          raise ValueError('количество: не более 2 знаков после запятой')

        if not can_edit_sale(doc_date):
          raise ValueError(
              f'дата {doc_date:%d.%m.%Y} закрыта для редактирования'
          )

        bucket = groups.setdefault((doc_date, client_id), {})
        bucket[item_id] = bucket.get(item_id, Decimal('0')) + qty
      except ValueError as e:
        errors.append(f'строка {idx}: {e}')

    if errors:
      shown = '; '.join(errors[:15])
      more = f' и еще {len(errors) - 15}' if len(errors) > 15 else ''
      flash(f'Накладные не загружены, исправьте файл: {shown}{more}', 'danger')
      return redirect(url_for('sales'))
    if not groups:
      flash('В файле нет строк с данными.', 'danger')
      return redirect(url_for('sales'))

    cursor.execute('SELECT pg_advisory_xact_lock(727003)')
    created = []
    user_id = session.get('user_id')
    for doc_date, client_id in sorted(groups, key=lambda k: (k[0], k[1])):
      cursor.execute('SELECT doc_number FROM sales_docs ORDER BY id DESC LIMIT 1')
      last_doc = cursor.fetchone()
      last_num = ''.join(filter(str.isdigit, str(last_doc['doc_number']))) if last_doc else ''
      doc_number = f'{int(last_num) + 1:06d}' if last_num else '001001'

      cursor.execute(
          'INSERT INTO sales_docs (doc_number, date, client_id, organization, user_id)'
          ' VALUES (%s, %s, %s, %s, %s) RETURNING id',
          (doc_number, doc_date, client_id, ORGANIZATION_NAME, user_id),
      )
      doc_id = cursor.fetchone()['id']
      for item_id, qty in groups[(doc_date, client_id)].items():
        cursor.execute(
            'INSERT INTO sales_items (doc_id, item_id, quantity) VALUES (%s, %s, %s)',
            (doc_id, item_id, qty),
        )
      created.append(doc_number)

    conn.commit()
    flash(
        f'Импортировано накладных: {len(created)} '
        f'(№ {created[0]} – № {created[-1]}).',
        'success',
    )
  except Exception as e:
    conn.rollback()
    flash(f'Ошибка при импорте: {e}', 'danger')
  finally:
    cursor.close()
    conn.close()
  return redirect(url_for('sales'))


@app.route('/api/item_by_barcode')
def item_by_barcode():
  current_org = ORGANIZATION_NAME
  code = request.args.get('code', '').strip().upper()
  if not code or len(code) > 50:
    return jsonify({'ok': False, 'error': 'Штрихкод не указан'}), 400
  conn = get_db_connection()
  cursor = conn.cursor()
  try:
    cursor.execute(
        'SELECT id, name, unit, barcode FROM items'
        ' WHERE organization = %s AND barcode = %s',
        (current_org, code),
    )
    item = cursor.fetchone()
  finally:
    cursor.close()
    conn.close()
  if not item:
    return jsonify({'ok': False, 'error': f'Штрихкод {code} не найден'}), 404
  return jsonify({
      'ok': True,
      'item': {
          'id': item['id'],
          'name': item['name'],
          'unit': item['unit'],
          'barcode': item['barcode'],
      },
  })


@app.route('/directory/labels')
def print_labels():
  if session.get('user_role') != 'admin':
    return redirect(url_for('sales'))
  current_org = ORGANIZATION_NAME
  try:
    ids = [int(x) for x in request.args.get('ids', '').split(',') if x.strip()]
  except ValueError:
    ids = []
  if not ids:
    flash('Выберите товары для печати этикеток', 'warning')
    return redirect(url_for('directory'))
  conn = get_db_connection()
  cursor = conn.cursor()
  try:
    cursor.execute(
        'SELECT id, name, unit, barcode FROM items'
        ' WHERE organization = %s AND id = ANY(%s) AND barcode IS NOT NULL'
        ' ORDER BY name',
        (current_org, ids),
    )
    labels = cursor.fetchall()
  finally:
    cursor.close()
    conn.close()
  return render_template('labels.html', labels=labels, current_org=current_org)


@app.route('/directory/add_item', methods=['POST'])
def add_item():
  if session.get('user_role') != 'admin':
    return redirect(url_for('sales'))
  current_org = ORGANIZATION_NAME
  name = request.form['name'].strip()
  unit = request.form['unit'].strip()

  price_raw = request.form.get('price', '').strip()
  price = float(price_raw) if price_raw else 0.0
  effective_date = request.form.get('effective_date', '').strip()

  conn = get_db_connection()
  cursor = conn.cursor()
  try:
    barcode = next_barcode(cursor, current_org)
    cursor.execute(
        'INSERT INTO items (name, unit, organization, barcode)'
        ' VALUES (%s, %s, %s, %s) RETURNING id',
        (name, unit, current_org, barcode),
    )
    row = cursor.fetchone()
    item_id = row['id'] if isinstance(row, dict) else row[0]

    if effective_date and price_raw:
      cursor.execute(
          'INSERT INTO item_prices (item_id, price, effective_date) VALUES'
          ' (%s, %s, %s)',
          (item_id, price, effective_date),
      )

    conn.commit()
    flash(f'Номенклатура успешно добавлена (штрихкод {barcode})', 'success')
  except Exception as e:
    conn.rollback()
    flash(f'Ошибка: {e}', 'danger')
  finally:
    cursor.close()
    conn.close()
  return redirect(url_for('directory'))


# --- Добавленный маршрут для редактирования товара ---
@app.route('/directory/edit_item/<int:id>', methods=['POST'])
def edit_item(id):
  if session.get('user_role') != 'admin':
    flash('Доступно только администратору', 'danger')
    return redirect(url_for('sales'))

  name = request.form.get('name', '').strip()
  unit = request.form.get('unit', '').strip()
  price_raw = request.form.get('price', '').strip()
  effective_date = request.form.get('effective_date', '').strip()

  if not name or not unit:
    flash('Заполните наименование и единицу измерения!', 'warning')
    return redirect(url_for('directory'))

  conn = get_db_connection()
  cursor = conn.cursor()
  try:
    cursor.execute(
        'UPDATE items SET name = %s, unit = %s WHERE id = %s',
        (name, unit, id),
    )

    if price_raw and effective_date:
      price = float(price_raw)
      cursor.execute(
          'INSERT INTO item_prices (item_id, price, effective_date) VALUES'
          ' (%s, %s, %s)',
          (id, price, effective_date),
      )

    conn.commit()
    flash('Товар успешно обновлен!', 'success')
  except Exception as e:
    conn.rollback()
    flash(f'Ошибка при обновлении товара: {e}', 'danger')
  finally:
    cursor.close()
    conn.close()
  return redirect(url_for('directory'))


@app.route('/directory/update_price/<int:item_id>', methods=['POST'])
def update_price(item_id):
  if session.get('user_role') != 'admin':
    return redirect(url_for('sales'))
  price = float(request.form.get('price', 0))
  eff_date = request.form.get('effective_date')
  conn = get_db_connection()
  cursor = conn.cursor()
  try:
    cursor.execute(
        'INSERT INTO item_prices (item_id, price, effective_date) VALUES'
        ' (%s, %s, %s)',
        (item_id, price, eff_date),
    )
    conn.commit()
    flash('Цена успешно обновлена!', 'success')
  except Exception as e:
    conn.rollback()
    flash(f'Ошибка: {e}', 'danger')
  finally:
    cursor.close()
    conn.close()
  return redirect(url_for('directory'))


@app.route('/directory/delete_item/<int:id>', methods=['POST'])
def delete_item(id):
  if session.get('user_role') != 'admin':
    return redirect(url_for('sales'))
  conn = get_db_connection()
  cursor = conn.cursor()
  cursor.execute('DELETE FROM items WHERE id = %s', (id,))
  conn.commit()
  cursor.close()
  conn.close()
  flash('Номенклатура удалена', 'info')
  return redirect(url_for('directory'))


@app.route('/directory/add_client', methods=['POST'])
def add_client():
  if session.get('user_role') != 'admin':
    return redirect(url_for('sales'))
  category = request.form['category'].strip()
  client_number = int(request.form['client_number'])
  conn = get_db_connection()
  cursor = conn.cursor()
  try:
    cursor.execute(
        'INSERT INTO clients (category, client_number) VALUES (%s, %s)',
        (category, client_number),
    )
    conn.commit()
    flash('МТТ успешно добавлен', 'success')
  except psycopg2.errors.UniqueViolation:
    conn.rollback()
    flash(f'МТТ №{client_number} уже существует!', 'warning')
  except Exception as e:
    conn.rollback()
    flash(f'Ошибка: {e}', 'danger')
  finally:
    cursor.close()
    conn.close()
  return redirect(url_for('directory'))


@app.route('/directory/delete_client/<int:id>', methods=['POST'])
def delete_client(id):
  if session.get('user_role') != 'admin':
    return redirect(url_for('sales'))
  conn = get_db_connection()
  cursor = conn.cursor()
  cursor.execute('DELETE FROM clients WHERE id = %s', (id,))
  conn.commit()
  cursor.close()
  conn.close()
  flash('МТТ удален', 'info')
  return redirect(url_for('directory'))


@app.route('/api/external/sales/create-draft', methods=['POST', 'OPTIONS'])
def create_draft_sale_from_raznaryadka():
  if request.method == 'OPTIONS':
    response = make_response()
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
    return response, 200

  try:
    data = request.get_json()
    if not data:
      return jsonify({'error': 'Данные не получены'}), 400

    target_date = data.get('date')
    mtt_name = data.get('mtt_name')
    items = data.get('items', [])
    organization = ORGANIZATION_NAME

    if not target_date or not mtt_name or not items:
      return (
          jsonify({
              'error': (
                  'Неполные данные (требуются date, mtt_name и список items)'
              )
          }),
          400,
      )

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
            SELECT id FROM clients 
            WHERE LOWER(category) LIKE %s
            LIMIT 1;
        """,
        (f'%{mtt_name.lower()}%',),
    )
    client_row = cursor.fetchone()

    if not client_row:
      mtt_num_match = re.search(r'\d+', mtt_name)
      if mtt_num_match:
        num = int(mtt_num_match.group())
        cursor.execute(
            'SELECT id FROM clients WHERE client_number = %s LIMIT 1;', (num,)
        )
        client_row = cursor.fetchone()

    if not client_row:
      cursor.close()
      conn.close()
      return jsonify({'error': f"МТТ '{mtt_name}' не найден в системе отгрузок"}), 404

    client_id = client_row['id']

    cursor.execute(
        'SELECT doc_number FROM sales_docs ORDER BY id DESC LIMIT 1'
    )
    last_doc = cursor.fetchone()
    if not last_doc:
      doc_number = '001001'
    else:
      last_num = ''.join(
          filter(str.isdigit, str(last_doc['doc_number']))
      )
      doc_number = f'{int(last_num) + 1:06d}' if last_num else '001001'

    cursor.execute(
        """
            INSERT INTO sales_docs (doc_number, date, organization, client_id, status)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id;
        """,
        (doc_number, target_date, organization, client_id, 'Черновик'),
    )
    doc_id = cursor.fetchone()['id']

    for item in items:
      prod_name = item.get('product')
      qty = float(item.get('qty', 0.0))
      if qty <= 0:
        continue

      cursor.execute(
          'SELECT id FROM items WHERE organization = %s AND LOWER(name) = %s LIMIT 1;',
          (organization, prod_name.lower().strip()),
      )
      item_row = cursor.fetchone()

      if item_row:
        item_id = item_row['id']
        cursor.execute(
            """
                    INSERT INTO sales_items (doc_id, item_id, quantity)
                    VALUES (%s, %s, %s);
                """,
            (doc_id, item_id, qty),
        )

    conn.commit()
    cursor.close()
    conn.close()

    response = jsonify({
        'status': 'success',
        'doc_id': doc_id,
        'message': f'Черновик накладной №{doc_number} успешно создан!',
    })
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response, 201

  except Exception as e:
    print(f'❌ Ошибка при создании черновика накладной: {e}')
    response = jsonify({'error': str(e)})
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response, 500


if __name__ == '__main__':
  port = int(os.environ.get('PORT', 5000))
  app.run(host='0.0.0.0', port=port)
