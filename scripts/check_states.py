# 查 mi_fitness 实体最新状态(新 schema: entity_id 在 states_meta,需 join)
import base64

import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('192.168.10.211', username='root', password='orangepi', timeout=10)

PY = r'''
import sqlite3, time
db = sqlite3.connect("file:/config/home-assistant_v2.db?mode=ro", uri=True)
cur = db.cursor()
rows = cur.execute("""
SELECT sm.entity_id, s.state, s.last_updated_ts
FROM states s
JOIN states_meta sm ON sm.metadata_id = s.metadata_id
JOIN (
  SELECT s2.metadata_id AS mid, MAX(s2.last_updated_ts) AS m
  FROM states s2
  JOIN states_meta sm2 ON sm2.metadata_id = s2.metadata_id
  WHERE sm2.entity_id LIKE 'sensor.%'
  GROUP BY s2.metadata_id
) t ON s.metadata_id = t.mid AND s.last_updated_ts = t.m
WHERE sm.entity_id LIKE '%'
ORDER BY sm.entity_id
""").fetchall()
for eid, st, ts in rows:
    t = time.strftime('%H:%M:%S', time.localtime(ts)) if ts else '-'
    print(f"{t}  {eid:<58} {st}")
print("TOTAL", len(rows))
'''

b64 = base64.b64encode(PY.encode()).decode()
cmd = f'docker exec homeassistant sh -c "echo {b64} | base64 -d | python3"'
_, out, err = c.exec_command(cmd, timeout=120)
print(out.read().decode('utf-8', 'replace') or '(无输出)')
e = err.read().decode('utf-8', 'replace').strip()
if e:
    print('ERR:', e[:600])
c.close()
