from flask import Flask, render_template, request, jsonify
import sqlite3
import os

app = Flask(__name__)
DB_PATH = 'shelter.db'

def init_db():
    if not os.path.exists(DB_PATH):
        conn = sqlite3.connect(DB_PATH)
        with open('schema.sql', 'r', encoding='utf-8') as f:
            conn.executescript(f.read())
        conn.commit()
        conn.close()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_items_status(conn):
    cur = conn.cursor()

    # Все предметы
    cur.execute("SELECT DISTINCT item_name, item_image FROM modules_requirements")
    all_items = cur.fetchall()

    # Текущий инвентарь
    cur.execute("SELECT item_name, have FROM inventory")
    have_map = {row['item_name']: row['have'] for row in cur.fetchall()}

    # Текущие уровни модулей
    cur.execute("SELECT module_name, current_level FROM player_progress")
    progress = {row['module_name']: row['current_level'] for row in cur.fetchall()}

    items = []
    for item in all_items:
        name = item['item_name']
        image = item['item_image']
        have = have_map.get(name, 0)

        # Сколько нужно для уровней СТРОГО выше текущего
        cur.execute("""
            SELECT SUM(mr.quantity) as need_total
            FROM modules_requirements mr
            LEFT JOIN player_progress pp ON mr.module_name = pp.module_name
            WHERE mr.item_name = ?
              AND (pp.current_level IS NULL OR mr.level > pp.current_level)
        """, (name,))
        need_row = cur.fetchone()
        need = need_row['need_total'] or 0
        left = max(0, need - have)

        items.append({
            'item_name': name,
            'item_image': image,
            'need': need,
            'have': have,
            'left': left
        })
    items.sort(key=lambda x: x['item_name'].lower())
    return items

@app.route('/')
def index():
    conn = get_db()
    cur = conn.cursor()

    # Модули с макс. уровнем
    cur.execute("""
        SELECT module_name, MAX(level) as max_level
        FROM modules_requirements
        GROUP BY module_name
    """)
    modules_raw = cur.fetchall()

    cur.execute("SELECT module_name, current_level FROM player_progress")
    progress = {row['module_name']: row['current_level'] for row in cur.fetchall()}

    modules = [
        {
            'name': m['module_name'],
            'max_level': m['max_level'],
            'current_level': progress.get(m['module_name'], 0)
        }
        for m in modules_raw
    ]

    items = get_items_status(conn)
    conn.close()
    # В конце функции index(), ПЕРЕД return render_template(...)

    # === Квесты (отдельное соединение) ===
    conn_q = get_db()
    cur_q = conn_q.cursor()

    try:
        # Получаем скрытых торговцев
        cur_q.execute("SELECT trader_id FROM hidden_traders")
        hidden_trader_ids = {row[0] for row in cur_q.fetchall()}

        # Торговцы + их квесты
        cur_q.execute("""
            SELECT t.id AS trader_id, t.name AS trader_name,
                q.id AS quest_id, q.name AS quest_name, q.is_completed
            FROM traders t
            LEFT JOIN quests q ON t.id = q.trader_id
            ORDER BY t.name, q.id
        """)
        rows = cur_q.fetchall()

        # Группируем по торговцам
        traders_data = {}
        for row in rows:
            tid = row['trader_id']
            if tid not in traders_data:
                traders_data[tid] = {
                    'id': tid,
                    'name': row['trader_name'],
                    'is_hidden': tid in hidden_trader_ids,
                    'quests': []
                }
            if row['quest_id'] is not None:
                traders_data[tid]['quests'].append({
                    'id': row['quest_id'],
                    'name': row['quest_name'],
                    'is_completed': bool(row['is_completed'])
                })

        # Предметы для квестов
        cur_q.execute("SELECT item_name, have FROM inventory")
        have_map = {row['item_name']: row['have'] for row in cur_q.fetchall()}

        need_items = {}
        for trader in traders_data.values():
            if trader['is_hidden']:
                continue
            for quest in trader['quests']:
                if quest['is_completed']:
                    continue
                cur_q.execute("""
                    SELECT item_name, item_image, SUM(quantity) as qty
                    FROM quest_requirements
                    WHERE quest_id = ?
                    GROUP BY item_name, item_image
                """, (quest['id'],))
                for req in cur_q.fetchall():
                    name = req['item_name']
                    img = req['item_image']
                    qty = req['qty']
                    if name not in need_items:
                        need_items[name] = {'item_name': name, 'item_image': img, 'need': 0}
                    need_items[name]['need'] += qty

        quest_items = []
        for name, info in need_items.items():
            have = have_map.get(name, 0)
            left = max(0, info['need'] - have)
            quest_items.append({
                'item_name': name,
                'item_image': info['item_image'],
                'need': info['need'],
                'have': have,
                'left': left
            })
        quest_items.sort(key=lambda x: x['item_name'].lower())

    finally:
        conn_q.close()  # ← Обязательно закрываем!
    return render_template('index.html',
    modules=modules, items=items,
    traders=list(traders_data.values()), quest_items=quest_items)


@app.route('/update_level', methods=['POST'])
def update_level():
    try:
        data = request.json
        if not data or 'module' not in data or 'level' not in data:
            return jsonify(success=False, error="Missing 'module' or 'level' in request"), 400

        module = str(data['module']).strip()
        if not module:
            return jsonify(success=False, error="Module name cannot be empty"), 400

        try:
            new_level = int(data['level'])
        except (ValueError, TypeError):
            return jsonify(success=False, error="'level' must be an integer"), 400

        conn = get_db()
        cur = conn.cursor()

        # Текущий уровень
        cur.execute("SELECT current_level FROM player_progress WHERE module_name = ?", (module,))
        row = cur.fetchone()
        old_level = row['current_level'] if row else 0

        if new_level < 0:
            conn.close()
            return jsonify(success=False, error="Level cannot be negative"), 400

        if new_level == old_level:
            conn.close()
            return jsonify(success=True)

        # Если уровень повысился — проверим, хватает ли ресурсов
        if new_level > old_level:
            cur.execute("""
                SELECT item_name, SUM(quantity) as qty
                FROM modules_requirements
                WHERE module_name = ? AND level > ? AND level <= ?
                GROUP BY item_name
            """, (module, old_level, new_level))
            items_to_deduct = cur.fetchall()

            # Проверим инвентарь
            insufficient = []
            for item in items_to_deduct:
                name = item['item_name']
                qty_needed = item['qty']
                cur.execute("SELECT have FROM inventory WHERE item_name = ?", (name,))
                inv = cur.fetchone()
                have = inv['have'] if inv else 0
                if have < qty_needed:
                    insufficient.append(f"{name} (нужно: {qty_needed}, есть: {have})")

            if insufficient:
                conn.close()
                return jsonify(
                    success=False,
                    error="Недостаточно предметов для повышения уровня",
                    missing=insufficient
                ), 400

            # Списываем
            for item in items_to_deduct:
                name = item['item_name']
                qty = item['qty']
                cur.execute("""
                    UPDATE inventory
                    SET have = have - ?
                    WHERE item_name = ?
                """, (qty, name))

        # Сохраняем новый уровень
        cur.execute("""
            INSERT OR REPLACE INTO player_progress (module_name, current_level)
            VALUES (?, ?)
        """, (module, new_level))

        conn.commit()
        conn.close()
        return jsonify(success=True)
    except Exception as e:
        import traceback
        print("ERROR in /update_level:", traceback.format_exc())
        return jsonify(success=False, error="Internal error: " + str(e)), 500


@app.route('/update_have', methods=['POST'])
def update_have():
    try:
        data = request.json
        print("📩 /update_have received JSON:", data)  # ← ВРЕМЕННЫЙ ЛОГ
        if not data or 'item' not in data or 'have' not in data:
            return jsonify(success=False, error="Missing 'item' or 'have' in request"), 400

        item_name = str(data['item']).strip()
        print("📦 item_name after strip:", repr(item_name))  # ← ВРЕМЕННЫЙ ЛОГ
        if not item_name:
            return jsonify(success=False, error="Item name cannot be empty"), 400

        try:
            have = int(data['have'])
        except (ValueError, TypeError):
            return jsonify(success=False, error="'have' must be an integer"), 400

        if have < 0:
            have = 0

        conn = get_db()
        cur = conn.cursor()

        cur.execute("SELECT item_image FROM modules_requirements WHERE item_name = ? LIMIT 1", (item_name,))
        row = cur.fetchone()
        image = row['item_image'] if row else 'unknown.png'
        print("🖼️ Found image:", image)  # ← ВРЕМЕННЫЙ ЛОГ

        print(f"💾 INSERT OR REPLACE INTO inventory: {item_name}, {image}, {have}")
        cur.execute("""
            INSERT OR REPLACE INTO inventory (item_name, item_image, have)
            VALUES (?, ?, ?)
        """, (item_name, image, have))

        conn.commit()
        print("✅ COMMIT successful")  # ← ВРЕМЕННЫЙ ЛОГ
        conn.close()
        return jsonify(success=True)
    except Exception as e:
        import traceback
        print("❌ ERROR in /update_have:", traceback.format_exc())
        return jsonify(success=False, error="Internal error: " + str(e)), 500


@app.route('/items_table')
def items_table():
    try:
        conn = get_db()
        items = get_items_status(conn)
        conn.close()
        return jsonify(items=items)
    except Exception as e:
        import traceback
        print("ERROR in /items_table:", traceback.format_exc())
        return jsonify(items=[]), 500

@app.route('/next_level_items')
def next_level_items():
    try:
        conn = get_db()
        cur = conn.cursor()

        # Текущие уровни
        cur.execute("SELECT module_name, current_level FROM player_progress")
        progress = {row['module_name']: row['current_level'] for row in cur.fetchall()}

        # Макс. уровни
        cur.execute("SELECT module_name, MAX(level) as max_level FROM modules_requirements GROUP BY module_name")
        max_levels = {row['module_name']: row['max_level'] for row in cur.fetchall()}

        # Соберём требования для (current_level + 1) каждого модуля
        module_requirements = []  # [{'module': ..., 'next_level': ..., 'items': [...]}]
        all_items = {}  # {'item_name': {need_total, entries: [{'module','level','qty','image'}]}}

        for module, curr_lvl in progress.items():
            next_lvl = curr_lvl + 1
            max_lvl = max_levels.get(module, 0)
            if next_lvl > max_lvl:
                continue

            cur.execute("""
                SELECT item_name, item_image, quantity
                FROM modules_requirements
                WHERE module_name = ? AND level = ?
            """, (module, next_lvl))
            items_for_level = cur.fetchall()

            if not items_for_level:
                continue

            # Сохраняем требования модуля
            module_req = {
                'module': module,
                'next_level': next_lvl,
                'items': []
            }
            for row in items_for_level:
                name = row['item_name']
                image = row['item_image']
                qty = row['quantity']
                module_req['items'].append({'item_name': name, 'item_image': image, 'quantity': qty})

                # Агрегируем по предметам
                if name not in all_items:
                    all_items[name] = {
                        'item_name': name,
                        'item_image': image,
                        'need': 0,
                        'entries': []
                    }
                all_items[name]['need'] += qty
                all_items[name]['entries'].append({
                    'module': module,
                    'level': next_lvl,
                    'quantity': qty
                })

            module_requirements.append(module_req)

        # Получим инвентарь
        cur.execute("SELECT item_name, have FROM inventory")
        have_map = {row['item_name']: row['have'] for row in cur.fetchall()}

        # Формируем итоговый список предметов с деталями
        result = []
        for name, info in all_items.items():
            have = have_map.get(name, 0)
            left = max(0, info['need'] - have)
            result.append({
                'item_name': name,
                'item_image': info['item_image'],
                'need': info['need'],
                'have': have,
                'left': left,
                'entries': info['entries']  # ← вот она — разбивка по модулям!
            })

        result.sort(key=lambda x: x['item_name'])

        conn.close()
        return jsonify(items=result)
    except Exception as e:
        import traceback
        print("ERROR in /next_level_items:", traceback.format_exc())
        result.sort(key=lambda x: x['item_name'].lower())
        return jsonify(items=[]), 500

@app.route('/quests_data')
def quests_data():
    conn = get_db()
    cur = conn.cursor()

    # Получаем скрытых торговцев
    cur.execute("SELECT trader_id FROM hidden_traders")
    hidden_trader_ids = {row[0] for row in cur.fetchall()}

    # Торговцы + их квесты
    cur.execute("""
        SELECT t.id AS trader_id, t.name AS trader_name,
               q.id AS quest_id, q.name AS quest_name, q.is_completed
        FROM traders t
        LEFT JOIN quests q ON t.id = q.trader_id
        ORDER BY t.name, q.id
    """)
    rows = cur.fetchall()

    # Группируем по торговцам
    traders = {}
    for row in rows:
        tid = row['trader_id']
        if tid not in traders:
            traders[tid] = {
                'id': tid,
                'name': row['trader_name'],
                'is_hidden': tid in hidden_trader_ids,
                'quests': []
            }
        if row['quest_id'] is not None:
            traders[tid]['quests'].append({
                'id': row['quest_id'],
                'name': row['quest_name'],
                'is_completed': bool(row['is_completed'])
            })

    # Получаем инвентарь для расчёта "нужно"
    cur.execute("SELECT item_name, have FROM inventory")
    have_map = {row['item_name']: row['have'] for row in cur.fetchall()}

    # Считаем, что нужно по НЕВЫПОЛНЕННЫМ квестам (и не от скрытых торговцев!)
    need_items = {}
    for trader in traders.values():
        if trader['is_hidden']:
            continue
        for quest in trader['quests']:
            if quest['is_completed']:
                continue
            cur.execute("""
                SELECT item_name, item_image, SUM(quantity) as qty
                FROM quest_requirements
                WHERE quest_id = ?
                GROUP BY item_name, item_image
            """, (quest['id'],))
            for req in cur.fetchall():
                name = req['item_name']
                img = req['item_image']
                qty = req['qty']
                if name not in need_items:
                    need_items[name] = {'item_name': name, 'item_image': img, 'need': 0}
                need_items[name]['need'] += qty

    # Формируем итоговый список предметов
    items = []
    for name, info in need_items.items():
        have = have_map.get(name, 0)
        left = max(0, info['need'] - have)
        items.append({
            'item_name': name,
            'item_image': info['item_image'],
            'need': info['need'],
            'have': have,
            'left': left
        })
    items.sort(key=lambda x: x['item_name'].lower())

    conn.close()
    return jsonify(
        traders=list(traders.values()),
        items=items
    )


@app.route('/toggle_quest', methods=['POST'])
def toggle_quest():
    try:
        data = request.json
        quest_id = int(data['quest_id'])
        completed = bool(data['completed'])

        conn = get_db()
        cur = conn.cursor()

        if completed:
            # Проверим, хватает ли предметов
            cur.execute("""
                SELECT item_name, SUM(quantity) as qty
                FROM quest_requirements
                WHERE quest_id = ?
                GROUP BY item_name
            """, (quest_id,))
            requirements = cur.fetchall()

            insufficient = []
            for req in requirements:
                name = req['item_name']
                need = req['qty']
                cur.execute("SELECT have FROM inventory WHERE item_name = ?", (name,))
                have = cur.fetchone()
                if not have or have[0] < need:
                    insufficient.append(f"{name} (нужно: {need}, есть: {have[0] if have else 0})")

            if insufficient:
                conn.close()
                return jsonify(
                    success=False,
                    error="Недостаточно предметов",
                    missing=insufficient
                ), 400

            # Списываем
            for req in requirements:
                cur.execute("""
                    UPDATE inventory
                    SET have = have - ?
                    WHERE item_name = ?
                """, (req['qty'], req['item_name']))

        # Обновляем статус квеста
        cur.execute("""
            UPDATE quests
            SET is_completed = ?
            WHERE id = ?
        """, (1 if completed else 0, quest_id))

        conn.commit()
        conn.close()
        return jsonify(success=True)
    except Exception as e:
        import traceback
        print("ERROR in /toggle_quest:", traceback.format_exc())
        return jsonify(success=False, error=str(e)), 500



if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)