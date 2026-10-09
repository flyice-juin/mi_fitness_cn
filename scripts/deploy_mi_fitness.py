# 部署小米运动健康集成到 HA 服务器(192.168.10.211,docker 版 HA)
# 与 deploy_keep_fitness.py 同源结构,差异:
# - 不清理线上多余文件:本地仓库没有 www/(Lovelace 卡片)目录,线上可能有,
#   镜像清理会把卡片删掉弄坏面板 —— 只覆盖上传。
# - 重启后抓 mi_fitness 相关日志:配置错误 / EXERCISE 未识别形状 / 未知 key。
import hashlib
import time
from pathlib import Path

import paramiko

LOCAL = Path(__file__).resolve().parent.parent / 'custom_components' / 'mi_fitness'
REMOTE = '/opt/homeassistant/custom_components/mi_fitness'
ALLOWED_SUFFIXES = {'.py', '.json', '.yaml', '.png'}


def collect_files() -> list[str]:
    files = []
    for p in sorted(LOCAL.rglob('*')):
        if not p.is_file():
            continue
        if '__pycache__' in p.parts or p.suffix == '.pyc':
            continue
        if p.suffix not in ALLOWED_SUFFIXES:
            continue
        files.append(p.relative_to(LOCAL).as_posix())
    return files


FILES = collect_files()
print('待上传文件:')
for f in FILES:
    print('  ', f)
assert FILES, '本地未找到任何待上传文件'

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('192.168.10.211', username='root', password='orangepi', timeout=10)


def run(cmd, timeout=300):
    _, out, err = c.exec_command(cmd, timeout=timeout)
    code = out.channel.recv_exit_status()
    return code, out.read().decode('utf-8', 'replace').strip(), err.read().decode('utf-8', 'replace').strip()


code, o, _ = run(f'ls -la {REMOTE} 2>/dev/null; echo ---; find {REMOTE} -type f 2>/dev/null | head -40')
print('[0] 线上现有文件(上传前,确认不误删):')
print(o or '(目录不存在,将新建)')

print('[1] uploading integration sources ...')
sftp = c.open_sftp()
try:
    sftp.mkdir(REMOTE)
except IOError:
    pass
for rel in FILES:
    remote = f'{REMOTE}/{rel}'
    parent = remote.rsplit('/', 1)[0]
    if parent != REMOTE:
        try:
            sftp.mkdir(parent)
        except IOError:
            pass
    sftp.put(str(LOCAL / rel), remote)
    print(f'    put {rel}')
sftp.close()

print('[2] verifying sha256 ...')
bad = []
for rel in FILES:
    local_sha = hashlib.sha256((LOCAL / rel).read_bytes()).hexdigest()
    _, o, _ = run(f'sha256sum "{REMOTE}/{rel}" | cut -d" " -f1')
    if o.strip() != local_sha:
        bad.append(rel)
        print(f'    MISMATCH {rel}')
if bad:
    raise SystemExit(f'upload hash mismatch: {bad} — aborting')
print(f'    {len(FILES)} files all MATCH')

print('[3] restarting HA(期间 HA 不可用 3-5 分钟)...')
run('docker restart homeassistant', timeout=300)
up = False
for _ in range(30):
    time.sleep(12)
    _, o, _ = run('docker ps --filter name=homeassistant --format "{{.Status}}"')
    if 'Up' in o:
        up = True
        break
print('HA UP' if up else 'HA DOWN!')
if not up:
    raise SystemExit('HA 未恢复,请登录服务器检查 docker logs homeassistant')
time.sleep(75)

print('[4] verifying inside container ...')
_, o, _ = run(
    'docker exec homeassistant sh -c '
    f'"ls /config/custom_components/mi_fitness && '
    f'cd /config/custom_components/mi_fitness && python3 -m py_compile *.py && echo COMPILE_OK"'
)
print(o)
if 'COMPILE_OK' not in o:
    raise SystemExit('容器内编译检查失败')

print('[5] HA 日志中 mi_fitness 相关(重启后 8 分钟):')
_, o, _ = run(
    'docker logs homeassistant --since 8m 2>&1 | grep -i "mi_fitness\\|mi fitness" | tail -40'
)
print(o or '(无输出 — 无错误也无 EXERCISE 形状日志,可能首轮刷新还在进行)')

_, o, _ = run('docker logs homeassistant --since 8m 2>&1 | grep -B2 -A8 "Traceback" | head -60')
if o:
    print('[!] 发现 Traceback(可能是别的集成,需人工确认):')
    print(o)

c.close()
print('DONE — 到 HA 里检查「小米运动健康」实体的新状态')
