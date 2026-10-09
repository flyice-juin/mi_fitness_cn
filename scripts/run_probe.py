# 上传探针到 HA 服务器并在容器内执行,输出带回本地
import sys
from pathlib import Path

import paramiko

LOCAL = Path(__file__).resolve().parent.parent / 'tools' / 'probe_exercise.py'
REMOTE = '/opt/homeassistant/probe_exercise.py'

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('192.168.10.211', username='root', password='orangepi', timeout=10)
sftp = c.open_sftp()
sftp.put(str(LOCAL), REMOTE)
sftp.close()
print('probe uploaded')

_, out, err = c.exec_command(
    'docker exec homeassistant python3 /config/probe_exercise.py 2>&1', timeout=240
)
code = out.channel.recv_exit_status()
print(out.read().decode('utf-8', 'replace'))
if code != 0:
    print('EXIT CODE', code)
    print(err.read().decode('utf-8', 'replace')[:1000])

# 清理探针,不留凭证相关脚本在配置目录
c.exec_command(f'rm -f {REMOTE}')
c.close()
print('probe removed')
