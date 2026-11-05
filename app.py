from datetime import datetime
from decimal import Decimal, InvalidOperation
import io
import os
import uuid
import pandas as pd
import psycopg2
import psycopg2.extras
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
    jsonify,
)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'warehouse_super_secret_key_1c')

ORGANIZATIONS = {
    'OTABEK UMAR FAYZ': {
        'name': 'OTABEK UMAR FAYZ',
        'address': 'Бекобод тумани',
        'account': '20208000907017874001',
        'bank': 'ТИФ "Миллий банк" Бекобод БХМ.',
        'mfo': '00450',
        'inn': '311147751',
        'has_vat': False,
        'pin': '1111',
    },
    'ISMOIL-OMAD-FAYZ-555': {
        'name': 'ISMOIL-OMAD-FAYZ-555',
        'address': 'Бекобод тумани',
        'account': '20208000805690249001',
        'bank': 'ТИФ "Миллий банк" Бекобод БХМ',
        'mfo': '00450',
        'inn': '310748843',
        'has_vat': True,
        'pin': '2222',
    },
    'MUXSIN-FAYZ': {
        'name': 'MUXSIN-FAYZ',
        'address': 'Бекобод 13-даха Фаргона МФЙ',
        'account': '20208000005351182001',
        'bank': 'Миллий Банк. Бекобод БХМ',
        'mfo': '00450',
        'inn': '306242444',
        'has_vat': False,
        'pin': '3333',
    },
}


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
  """Следующий внутренний штрихкод SKU-000001 для организации.

  Берётся максимальный номер + 1 (а не COUNT), поэтому удаление товаров
  не приводит к повторам. Вызывать внутри транзакции: блокировка
  pg_advisory_xact_lock исключает одинаковые коды при одновременных запросах.
  """
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
    # Блокировка на время миграции: несколько воркеров gunicorn не должны
    # одновременно присваивать штрихкоды.
    cursor.execute('SELECT pg_advisory_xact_lock(727001)')
    cursor.execute('''
            CREATE TABLE IF NOT EXISTS items (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                unit VARCHAR(50) NOT NULL,
                organization VARCHAR(100) DEFAULT 'OTABEK UMAR FAYZ'
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
                organization VARCHAR(100) DEFAULT 'OTABEK UMAR FAYZ'
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
                organization VARCHAR(100) DEFAULT 'OTABEK UMAR FAYZ',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
    # --- Миграция: штрихкоды и защита от двойного сохранения (только добавления) ---
    cursor.execute('ALTER TABLE items ADD COLUMN IF NOT EXISTS barcode VARCHAR(50)')
    cursor.execute(
        'ALTER TABLE sales_docs ADD COLUMN IF NOT EXISTS submit_token VARCHAR(64)'
    )
    cursor.execute("""
        UPDATE items AS i
        SET barcode = 'SKU-' || LPAD((COALESCE(m.mx, 0) + n.rn)::text, 6, '0')
        FROM (
            SELECT id, organization,
                   ROW_NUMBER() OVER (PARTITION BY organization ORDER BY id) AS rn
            FROM items WHERE barcode IS NULL OR barcode = ''
        ) AS n
        LEFT JOIN (
            SELECT organization, MAX(SUBSTRING(barcode FROM 5)::int) AS mx
            FROM items WHERE barcode ~ '^SKU-[0-9]+$' GROUP BY organization
        ) AS m ON m.organization IS NOT DISTINCT FROM n.organization
        WHERE i.id = n.id
    """)
    cursor.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS ux_items_org_barcode'
        ' ON items (organization, barcode) WHERE barcode IS NOT NULL'
    )
    cursor.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS ux_sales_docs_submit_token'
        ' ON sales_docs (submit_token) WHERE submit_token IS NOT NULL'
    )
    conn.commit()
  except Exception as e:
    conn.rollback()
    print(f'Schema init error: {e}')
  finally:
    cursor.close()
    conn.close()


init_db_schema()


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
      date = request.form.get('date', '').strip()
      client_id_raw = request.form.get('client_id')
      token = request.form.get('submit_token', '').strip()[:64] or None

      if not date or not client_id_raw:
        raise ValueError('Заполните дату и выберите МТТ!')
      try:
        datetime.strptime(date, '%Y-%m-%d')
        client_id = int(client_id_raw)
      except ValueError:
        raise ValueError('Некорректная дата или МТТ!')

      cursor.execute('SELECT 1 FROM clients WHERE id = %s', (client_id,))
      if not cursor.fetchone():
        raise ValueError('Выбранный МТТ не найден!')

      item_ids = request.form.getlist('item_id[]')
      quantities = request.form.getlist('quantity[]')

      # Разрешены только товары текущей организации
      cursor.execute(
          'SELECT id FROM items WHERE organization = %s', (current_org,)
      )
      allowed_ids = {r['id'] for r in cursor.fetchall()}

      merged = {}  # item_id -> суммарное количество (повторы суммируются)
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
        if qty > Decimal('9999999999.99'):
          raise ValueError('Слишком большое количество!')
        if item_id not in allowed_ids:
          raise ValueError('Товар не найден в вашей организации!')
        merged[item_id] = merged.get(item_id, Decimal('0')) + qty

      if not merged:
        raise ValueError('Добавьте хотя бы одну позицию товара!')

      # Сериализуем создание накладных: нумерация и защита от двойного клика
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
          ' submit_token) VALUES (%s, %s, %s, %s, %s) RETURNING id',
          (doc_number, date, client_id, current_org, token),
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

  filter_date = request.args.get('filter_date', '').strip()
  filter_client = request.args.get('filter_client', '').strip()
  filter_doc = request.args.get('filter_doc', '').strip()

  query = """
        SELECT d.id, d.doc_number, d.date, c.category, c.client_number, d.organization,
               COALESCE((SELECT STRING_AGG(i.name || ' (' || TO_CHAR(si.quantity, 'FM999999999.00') || ' ' || i.unit || ')', ', ') 
                FROM sales_items si JOIN items i ON si.item_id = i.id WHERE si.doc_id = d.id), '') as items_summary
        FROM sales_docs d
        JOIN clients c ON d.client_id = c.id
        WHERE d.organization = %s
    """
  params = [current_org]

  if filter_date:
    query += ' AND d.date = %s'
    params.append(filter_date)
  if filter_client:
    query += ' AND d.client_id = %s'
    params.append(int(filter_client))
  if filter_doc:
    query += ' AND d.doc_number ILIKE %s'
    params.append(f'%{filter_doc}%')

  query += ' ORDER BY d.date DESC, d.id DESC'

  cursor.execute(query, params)
  sales_docs = cursor.fetchall()

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

  cursor.close()
  conn.close()

  return render_template(
      'sales.html',
      sales_docs=sales_docs,
      items=items,
      clients=clients,
      contracts=contracts,
      current_org=current_org,
      filter_date=filter_date,
      filter_client=filter_client,
      filter_doc=filter_doc,
      submit_token=uuid.uuid4().hex,
  )


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
          'UPDATE sales_docs SET date = %s, client_id = %s, organization = %s'
          ' WHERE id = %s',
          (date, client_id, current_org, doc_id),
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

  org_key = (
      doc['organization']
      if doc['organization'] in ORGANIZATIONS
      else 'OTABEK UMAR FAYZ'
  )
  org_data = ORGANIZATIONS[org_key]

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
  current_org = session.get('current_org')
  if not current_org:
    return redirect(url_for('index'))

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
  cursor.close()
  conn.close()

  return render_template(
      'directory.html',
      items=items,
      prices_map=prices_map,
      clients=clients,
      contracts=contracts,
      current_org=current_org,
  )


@app.route('/directory/add_contract', methods=['POST'])
def add_contract():
  current_org = session.get('current_org')
  if not current_org:
    return redirect(url_for('index'))
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
  current_org = session.get('current_org', 'OTABEK UMAR FAYZ')
  conn = get_db_connection()
  cursor = conn.cursor()
  cursor.execute(
      """
        SELECT d.doc_number, d.date, c.category || ' — МТТ №' || c.client_number as client_name,
               i.name as item_name, si.quantity, i.unit
        FROM sales_docs d JOIN clients c ON d.client_id = c.id
        JOIN sales_items si ON si.doc_id = d.id JOIN items i ON si.item_id = i.id
        WHERE d.organization = %s ORDER BY d.date DESC, d.id DESC
    """,
      (current_org,),
  )
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
        'Кол-во': f"{float(r['quantity']):.2f} {r['unit']}",
    })

  df = pd.DataFrame(data)
  if df.empty:
    df = pd.DataFrame(
        columns=['Номер накладной', 'Дата', 'МТТ', 'Номенклатура', 'Кол-во']
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
  cursor.execute('DELETE FROM sales_docs WHERE id = %s', (id,))
  conn.commit()
  cursor.close()
  conn.close()
  flash('Накладная отгрузки удалена', 'info')
  return redirect(url_for('sales'))


@app.route('/api/item_by_barcode')
def item_by_barcode():
  """Поиск товара по штрихкоду строго среди товаров текущей организации."""
  current_org = session.get('current_org')
  if not current_org or current_org not in ORGANIZATIONS:
    return jsonify({'ok': False, 'error': 'Требуется вход в организацию'}), 401
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
          'id': item['id'], 'name': item['name'],
          'unit': item['unit'], 'barcode': item['barcode'],
      },
  })


@app.route('/directory/labels')
def print_labels():
  """Печать этикеток: ?ids=1,2,3 (только товары текущей организации)."""
  current_org = session.get('current_org')
  if not current_org or current_org not in ORGANIZATIONS:
    return redirect(url_for('index'))
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
  current_org = session.get('current_org', 'OTABEK UMAR FAYZ')
  name = request.form['name'].strip()
  unit = request.form['unit'].strip()

  # Цена и дата цены теперь необязательны (для MUXSIN-FAYZ особенно)
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


@app.route('/directory/update_price/<int:item_id>', methods=['POST'])
def update_price(item_id):
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
  conn = get_db_connection()
  cursor = conn.cursor()
  cursor.execute('DELETE FROM clients WHERE id = %s', (id,))
  conn.commit()
  cursor.close()
  conn.close()
  flash('МТТ удален', 'info')
  return redirect(url_for('directory'))


if __name__ == '__main__':
  port = int(os.environ.get('PORT', 5000))
  app.run(host='0.0.0.0', port=port)
