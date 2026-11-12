"""Автотест демо-сценария из раздела 9 ТЗ. Меняет базу: после него запустите python seed.py."""
import sys, re
import os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tempfile
import app as A, db
TMP = tempfile.gettempdir()
from datetime import date, timedelta
A.SPOT_CHECK = 0
c = A.app.test_client()
T = A.today(); TD = T.isoformat()
def login(u):
    c.get('/logout'); r = c.post('/login', data={'login': u, 'password': 'demo'}); assert r.status_code == 302, u
def ok(path, **kw):
    r = c.get(path, **kw); assert r.status_code == 200, (path, r.status_code, r.data[:800]); return r.get_data(as_text=True)
step = lambda n, s: print(f'[{n}] {s}')

# 1. Вход по QR + PIN (без логина)
c.get('/logout')
t = ok('/f/iskander'); assert 'PIN' in t or 'pin' in t
r = c.post('/f/iskander', data={'pin': '0000'}); assert 'Неверный' in r.get_data(as_text=True)
r = c.post('/f/iskander', data={'pin': '1111'}); assert r.status_code == 302; url = r.headers['Location']
t = ok(url); assert 'Отчёт за' in t; step(1, f'QR+PIN → {url}')
ids = [int(x) for x in re.findall(r'class="tile[^"]*" data-id="(\d+)"', t)]
mids = re.findall(r'data-m="(\d+)"', t)
before = re.search(r'id="monthTotal">([^<]+)<', t).group(1)

# 2. 2–3 взвешивания с выбором способа, итоги пересчитываются
H = {'X-Fetch': '1'}
for wid, q, m in [(ids[0], '3.5', mids[0]), (ids[0], '2', mids[2]), (ids[1], '1.25', mids[1])]:
    j = c.post(url, data={'action': 'add', 'waste_type_id': wid, 'qty': q, 'method_id': m}, headers=H).get_json(); assert j['ok'], j
assert not c.post(url, data={'action': 'add', 'waste_type_id': ids[0], 'qty': '0'}, headers=H).get_json()['ok']
assert not c.post(url, data={'action': 'add', 'waste_type_id': 99999, 'qty': '1'}, headers=H).get_json()['ok']
fut = (T + timedelta(days=3)).isoformat()
assert c.get(url.replace(TD, fut)).status_code == 302   # будущая дата → редирект на сегодня
t = ok(url); day = re.search(r'id="dayTotal">([^<]+)<', t).group(1); after = re.search(r'id="monthTotal">([^<]+)<', t).group(1)
assert day == '6,75', day; step(2, f'3 взвешивания: итог дня {day} кг, месяц {before} → {after}')
# удаление записи
iid = re.findall(r'name="item_id" value="(\d+)"', t)[0]
c.post(url, data={'action': 'delete', 'item_id': iid}); t = ok(url)
day2 = re.search(r'id="dayTotal">([^<]+)<', t).group(1); step('2b', f'удалили запись → итог дня {day2}')
j = c.post(url, data={'action': 'add', 'waste_type_id': ids[0], 'qty': '1.25', 'method_id': mids[0]}, headers=H).get_json(); assert j['ok']

# 3. Новый вид отхода
r = c.post('/org/1/waste-type', data={'name': 'Хлебные корки', 'unit': 'кг', 'method_id': mids[4]}, headers={'Referer': url}, follow_redirects=True)
t = r.get_data(as_text=True); assert 'Хлебные корки' in t
new_id = int(re.findall(r'data-id="(\d+)" data-name="Хлебные корки"', t)[0])
assert c.post(url, data={'action': 'add', 'waste_type_id': new_id, 'qty': '2', 'method_id': mids[4]}, headers=H).get_json()['ok']
step(3, 'добавлен вид «Хлебные корки», по нему сделана запись')

# 4. Отправка отчёта за месяц
r = c.post(url, data={'action': 'submit'}, follow_redirects=True); t = r.get_data(as_text=True)
assert 'отправлен' in t, t[:2000]; why = re.search(r'Причина: ([^<]*)', t)
step(4, f'отчёт отправлен на ручную проверку; причина: {why.group(1)[:120] if why else "-"}')
assert c.post(url, data={'action': 'add', 'waste_type_id': ids[0], 'qty': '1'}, headers=H).status_code == 409

# 5. Проверяющий возвращает
login('proverka'); t = ok('/review')
rid = int(db.get_db.__self__ if False else 0) or None
import sqlite3; con = sqlite3.connect(db.DB_PATH)
rid = con.execute("SELECT id FROM reports WHERE org_id=1 AND date=?", (T.replace(day=1).isoformat(),)).fetchone()[0]
assert f'/review/{rid}' in t
t = ok(f'/review/{rid}'); assert 'Хлебные корки' in t and 'Способ утилизации' in t
r = c.post(f'/review/{rid}', data={'action': 'return'}, follow_redirects=True); assert 'нужен комментарий' in r.get_data(as_text=True)
c.post(f'/review/{rid}', data={'action': 'return', 'comment': 'Уточните вес корок — проверьте весы'})
step(5, 'возвращён с комментарием')

# 6. Исправление и повторная отправка → принятие, история видна
login('iskander'); t = ok(url); assert 'Уточните вес корок' in t
iid = re.findall(r'name="item_id" value="(\d+)"', t)[0]
c.post(url, data={'action': 'delete', 'item_id': iid})
c.post(url, data={'action': 'add', 'waste_type_id': new_id, 'qty': '1.8', 'method_id': mids[4]}, headers=H)
t = c.post(url, data={'action': 'submit'}, follow_redirects=True).get_data(as_text=True); assert 'отправлен' in t and 'исправление после возврата' in t
login('proverka'); c.post(f'/review/{rid}', data={'action': 'accept'})
t = ok(f'/review/{rid}'); hist = re.findall(r'class="pill st-(\w+)"', t)
step(6, f'исправлено, принято; история: {hist}')

# 7. Реестр и фильтры
t = ok('/registry'); n_all = re.search(r'(\d+) записей', t).group(1)
t = ok(f'/registry?org=1&method={mids[4]}&from={TD}&to={TD}'); n_f = re.search(r'(\d+) записей', t).group(1)
t2 = ok(f'/registry?st=accepted'); n_acc = re.search(r'(\d+) записей', t2).group(1)
x = c.get('/registry?export=xlsx'); assert x.status_code == 200
open(os.path.join(TMP, 'reestr.xlsx'), 'wb').write(x.data)
step(7, f'реестр: всего {n_all}, фильтр орг+способ+дата {n_f}, только принятые {n_acc}; Excel {len(x.data)} байт')

# 8. Дашборд ≥3 графиков
t = ok('/dashboard'); canv = re.findall(r'<canvas id="(\w+)"', t); assert len(canv) >= 3
step(8, f'дашборд: графики {canv}')

# 9. Сводный отчёт за квартал Excel/PDF
ok('/report?period=quarter')
x = c.get('/report?period=quarter&export=xlsx'); open(os.path.join(TMP, 'svod.xlsx'), 'wb').write(x.data)
p = c.get('/report?period=quarter&export=pdf'); open(os.path.join(TMP, 'svod.pdf'), 'wb').write(p.data)
assert x.status_code == p.status_code == 200 and p.data[:4] == b'%PDF'
import openpyxl; wb = openpyxl.load_workbook(os.path.join(TMP, 'svod.xlsx')); step(9, f'квартал: xlsx листы {wb.sheetnames}, pdf {len(p.data)} байт')

# 10. Несдавшие
t = ok('/review'); late = t.count('не сдан'); t2 = ok('/dashboard')
step(10, f'контроль сдачи: «не сдан» ячеек {late}')

# Прочее
for path in ['/report?period=month', '/report?period=year', '/surplus', '/qr', '/review?tab=auto']: ok(path)
login('admin'); ok('/admin')
r = c.post('/admin', data={'what': 'method', 'name': 'Биогаз', 'recycled': 'on'}, follow_redirects=True); assert 'добавлен' in r.get_data(as_text=True)
r = c.post('/admin', data={'what': 'org_active', 'org_id': '5'}, follow_redirects=True); assert c.get('/f/kaf-thipp').status_code == 404
c.post('/admin', data={'what': 'org_active', 'org_id': '5'})
login('rukovodstvo'); ok('/dashboard'); assert c.get('/review').status_code == 403
login('kaf_tpp'); assert c.get(f'/form/1/{TD}').status_code == 403; ok('/history')
c.get('/lang/kk'); login('iskander'); ok(url); ok('/history')
print('ALL OK')
