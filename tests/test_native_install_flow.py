"""Native installer integration in an isolated DB; QR and systemd are simulated.

Run only as root on a dedicated temporary IvorySQL test instance. Never installs
system packages or registers real systemd services. Uses an existing venv read-only.
"""
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
import install
import provision

ROOT=Path(__file__).resolve().parent.parent

def main():
    if os.geteuid()!=0:
        print('跳过：该隔离安装测试需 root，所有系统操作均模拟。');return
    base=load_test_config()
    tag=uuid.uuid4().hex[:12];dbname='test_install_'+tag;role=dbname
    calls=[]
    original_run=install.run
    async def fake_login(target,config,group):
        config['weixin'].update(base_url='https://example.invalid',account_id='test-native-account',allowed_user_id='test-native-user')
        install.private_file(target/'etc/weixin.token','test-only-token\n',group)
    def configured(target,binary,password):
        # Use the normal provision.configure function so DDL and config are real.
        with patch.object(provision,'login_and_save',fake_login):
            return provision.configure(target,binary,None)
    def safe_run(args,**kwargs):
        values=[str(x) for x in args]
        calls.append(values)
        if values[0]=='systemctl' or values[1:4]==['-m','pip','install']:
            return subprocess.CompletedProcess(values,0)
        if values[0] in ('useradd','dpkg','dnf','yum','apt-get'):
            raise AssertionError('Unexpected global system operation')
        return original_run(args,**kwargs)
    def borrowed_venv(python,target):
        # All package installation is mocked; test the normal runuser migration/check.
        Path(target).symlink_to(Path(sys.prefix),target_is_directory=True)
    with tempfile.TemporaryDirectory() as tmp:
        parent=Path(tmp);parent.chmod(0o755)
        target=parent/'application';units=parent/'units';units.mkdir()
        answers=[base.db.host,str(base.db.port),base.db.user,dbname,'','CREATE '+dbname,'https://example.invalid','test-model','test-style-model']
        try:
            with patch.object(install,'SYSTEMD_DIR',units),patch.object(install,'UNIT_SEARCH_DIRS',(units,)),patch.object(install,'ensure_venv',borrowed_venv),patch.object(install,'database_service',return_value='isolated-ivorysql.service'),patch.object(install,'provision_app',configured),patch.object(install,'run',safe_run),patch('builtins.input',side_effect=answers),patch('getpass.getpass',side_effect=[base.db.password,'test-only-model-key']),patch.object(sys.stdin,'isatty',return_value=True),patch.object(sys,'argv',['install.py','--prepared','--install-dir',str(target),'--pg-bin',os.environ.get('TEST_PG_BIN','/usr/ivory-5/bin')]):
                assert install.main()==0
            loaded=load(target/'etc/config.ini')
            assert loaded.db.dbname==dbname and loaded.db.user==role
            assert loaded.weixin.allowed_user_id=='test-native-user'
            assert loaded.deepseek.api_key=='test-only-model-key'
            for file in (target/'etc').iterdir():
                assert file.stat().st_mode & 0o777==0o640
            unit=(units/'reminder-bot.service').read_text()
            assert str(target) in unit and 'isolated-ivorysql.service' in unit
            assert 'User=reminderbot' in unit and 'docker' not in unit.lower()
            timer=(units/'reminder-backup.timer').read_text()
            assert '03:37:00 Asia/Shanghai' in timer
            assert any(command[:3]==['systemctl','enable','--now'] for command in calls)
            with psycopg.connect(**loaded.db.kwargs()) as conn:
                assert conn.execute("SELECT count(*) FROM bot.schema_migrations").fetchone()[0]==4
            print('[通过] 原生安装编排：真实新库、配置权限、服务用户迁移与自检；模拟扫码和服务注册')
        finally:
            with psycopg.connect(**base.db.kwargs(),autocommit=True) as admin:
                if admin.execute('SELECT 1 FROM pg_database WHERE datname=%s',(dbname,)).fetchone():
                    admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(dbname)))
                if admin.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(role,)).fetchone():
                    admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))

if __name__=='__main__':main()
