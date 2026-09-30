"""New DB provisioning + backup/restore in an isolated IvorySQL instance only."""
from __future__ import annotations
import configparser
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'scripts'))
import psycopg
from psycopg import sql
from support import load_test_config
from app.config import load
from provision import create_database

ROOT=Path(__file__).resolve().parent.parent

def main():
    base=load_test_config()
    tag=uuid.uuid4().hex[:12]
    dbname='test_deploy_'+tag;role='test_role_'+tag;restore='test_restore_'+tag
    password='test-only-password'
    binary=Path(os.environ.get('TEST_PG_BIN','/usr/ivory-5/bin'))
    created=[]
    with psycopg.connect(**base.db.kwargs(),autocommit=True) as admin:
        try:
            with patch('builtins.input',return_value='CREATE '+dbname):
                create_database(admin,dbname,role,password)
            created.append(dbname)
            flags=admin.execute('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=%s',(role,)).fetchone()
            assert flags==(False,False,False),flags
            print('[通过] 同意后仅创建新的数据库和专用低权限角色')
            kwargs=dict(base.db.kwargs(),dbname=dbname,user=role,password=password)
            with psycopg.connect(**kwargs,autocommit=True) as conn:
                for file in sorted((ROOT/'migrations').glob('*.sql')):conn.execute(file.read_text())
                conn.execute("INSERT INTO bot.tasks(user_id,content) VALUES ('test-user','synthetic-backup-content')")
                try:conn.execute('CREATE DATABASE forbidden_db')
                except psycopg.errors.InsufficientPrivilege:pass
                else:raise AssertionError('app role can create database')
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);secret=root/'app.pw';secret.write_text(password);secret.chmod(0o600)
                config=configparser.ConfigParser(interpolation=None);config.read(ROOT/'config.example.ini')
                config['db'].update(host=base.db.host,port=str(base.db.port),dbname=dbname,user=role,password_file=str(secret))
                config['backup']={'pg_bin':str(binary),'directory':str(root/'backups'),'keep_days':'14'}
                path=root/'config.ini'
                with path.open('w') as stream:config.write(stream)
                env=dict(os.environ,REMINDER_CONFIG=str(path))
                # Backup must not require WeChat or model credentials.
                subprocess.run([sys.executable,str(ROOT/'scripts/backup.py')],check=True,env=env)
                dumps=list((root/'backups').glob('*.dump'));assert len(dumps)==1
                assert dumps[0].stat().st_mode & 0o777==0o600
                assert (root/'backups').stat().st_mode & 0o777==0o700
                admin.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(restore),sql.Identifier(role)))
                created.append(restore)
                subprocess.run([str(binary/'pg_restore'),'-h',base.db.host,'-p',str(base.db.port),'-U',role,'-d',restore,'--no-owner','--no-privileges',str(dumps[0])],env=dict(os.environ,PGPASSWORD=password),check=True)
                with psycopg.connect(**dict(kwargs,dbname=restore)) as restored:
                    assert restored.execute('SELECT content FROM bot.tasks').fetchall()==[('synthetic-backup-content',)]
                print('[通过] 新库迁移、最小权限、备份权限与独立库恢复，内容一致')
        finally:
            # Only drop names generated in this test, after all child connections are closed.
            for item in reversed(created):
                admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(item)))
            if admin.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(role,)).fetchone():
                admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))

if __name__=='__main__':main()
