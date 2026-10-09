# 从 HA 服务器拉回集成目录的最新文件(源码+卡片,排除 __pycache__),与本地仓库对比
import hashlib
from pathlib import Path

import paramiko

LOCAL = Path(__file__).resolve().parent.parent / 'custom_components' / 'mi_fitness'
REMOTE = '/opt/homeassistant/custom_components/mi_fitness'
TMP = Path(__file__).resolve().parent.parent / '_server_snapshot'

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('192.168.10.211', username='root', password='orangepi', timeout=10)
sftp = c.open_sftp()


def walk(remote_dir, rel_prefix=''):
    items = []
    for attr in sftp.listdir_attr(remote_dir):
        name = attr.filename
        rel = f'{rel_prefix}{name}'
        if name == '__pycache__':
            continue
        if attr.st_mode & 0o040000:  # dir
            items.extend(walk(f'{remote_dir}/{name}', rel + '/'))
        else:
            items.append(rel)
    return items


files = walk(REMOTE)
print(f'服务器 {len(files)} 个文件:')
changed, new, same = [], [], []
for rel in files:
    rpath = f'{REMOTE}/{rel}'
    sha_remote = hashlib.sha256(sftp.open(rpath, 'rb').read()).hexdigest()
    (TMP / rel).parent.mkdir(parents=True, exist_ok=True)
    sftp.get(rpath, str(TMP / rel))
    lpath = LOCAL / rel
    if not lpath.exists():
        new.append(rel)
    elif hashlib.sha256(lpath.read_bytes()).hexdigest() != sha_remote:
        changed.append(rel)
    else:
        same.append(rel)
    print(f'  {"SAME " if rel in same else "DIFF " if lpath.exists() else "NEW  "} {rel}')

print(f'\n相同 {len(same)} / 变更 {len(changed)} / 本地新增 {len(new)}')
c.close()
