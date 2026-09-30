"""Native pg_dump backup; password is never included in process arguments."""
import configparser
from datetime import datetime,timedelta
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from app.config import DEFAULT_CONFIG_PATH,_read_secret


def main():
    os.umask(0o077)
    raw=configparser.ConfigParser(interpolation=None)
    if not raw.read(DEFAULT_CONFIG_PATH):raise SystemExit("读取备份配置失败")
    db=raw["db"]
    binary=Path(raw.get('backup','pg_bin',fallback='/usr/ivory-5/bin'))
    directory=Path(os.environ.get('BACKUP_DIR',raw.get('backup','directory',fallback='/var/backups/reminder-bot')))
    keep=int(os.environ.get('KEEP_DAYS',raw.get('backup','keep_days',fallback='14')))
    if keep<1:raise SystemExit('KEEP_DAYS 至少为 1')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    if directory.stat().st_mode & 0o077:raise SystemExit('备份目录权限必须为 700')
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    target=directory/f'reminder-{stamp}.dump'
    temp=target.with_suffix('.dump.tmp')
    env=dict(os.environ,PGPASSWORD=_read_secret(db["password_file"]))
    subprocess.run([str(binary/'pg_dump'),'-h',db['host'],'-p',db['port'],'-U',db['user'],'-d',db['dbname'],'--format=custom','--no-owner','--no-privileges','--file',str(temp)],env=env,check=True)
    subprocess.run([str(binary/'pg_restore'),'--list',str(temp)],stdout=subprocess.DEVNULL,check=True)
    temp.chmod(0o600);temp.rename(target)
    cutoff=datetime.now().timestamp()-keep*86400
    files=sorted(directory.glob('reminder-*.dump'),key=lambda p:p.stat().st_mtime,reverse=True)
    for i,path in enumerate(files):
        if i>=keep or path.stat().st_mtime<cutoff:path.unlink()
    print(f'备份完成：{target.name}，{target.stat().st_size} 字节')

if __name__=='__main__':main()
