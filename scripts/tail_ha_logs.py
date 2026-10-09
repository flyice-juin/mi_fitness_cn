# 抓 HA 容器里 mi_fitness 相关日志
import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('192.168.10.211', username='root', password='orangepi', timeout=10)

for cmd in (
    "date '+%H:%M:%S'",
    "docker logs homeassistant --since 25m 2>&1 | grep -i 'mi fitness\\|mi_fitness' | tail -30",
):
    _, out, _ = c.exec_command(cmd, timeout=60)
    print('$', cmd)
    print(out.read().decode('utf-8', 'replace') or '(空)')
    print('---')
c.close()
