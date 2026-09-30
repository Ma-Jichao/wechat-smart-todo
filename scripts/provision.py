"""Interactive configuration and new-database provisioning for the native installer."""
from __future__ import annotations
import asyncio
import configparser
import getpass
import io
import os
from pathlib import Path
import pwd
import re
import secrets
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from install import confirm, private_file


def name(value):
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}',value):
        raise SystemExit('数据库名与角色名须为小写字母开头，最多 63 位，仅含字母、数字、下划线。')
    return value


def create_database(conn,dbname,role,password):
    """Only create new names, after explicit consent; never change existing roles."""
    import psycopg
    from psycopg import sql
    if conn.execute('SELECT 1 FROM pg_database WHERE datname=%s',(dbname,)).fetchone():
        raise SystemExit('该数据库已存在，拒绝复用或覆盖，请选择一个新的名字。')
    if conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(role,)).fetchone():
        raise SystemExit('该角色已存在，拒绝修改已有角色，请选择一个新的名字。')
    confirm('CREATE '+dbname,f'将创建新数据库 {dbname} 和专用低权限角色 {role}。不会改动现有数据库、角色或服务器配置。')
    conn.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}').format(sql.Identifier(role),sql.Literal(password)))
    conn.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(dbname),sql.Identifier(role)))
    conn.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(dbname)))


async def login_and_save(target,config,group):
    from wechatbot.auth import login
    from wechatbot.protocol import ILinkApi
    creds=await login(ILinkApi(),cred_path=target/'etc/credentials.json')
    config['weixin'].update(base_url=creds.base_url,account_id=creds.account_id,allowed_user_id=creds.user_id)
    private_file(target/'etc/weixin.token',creds.token+'\n',group)


def configure(target,pg_bin,fresh_admin_password):
    import psycopg
    config=configparser.ConfigParser(interpolation=None)
    config.read(target/'config.example.ini')
    group=pwd.getpwnam('reminderbot').pw_gid
    host='127.0.0.1';port=5432
    adminuser='ivorysql';adminpassword=fresh_admin_password
    if fresh_admin_password is None:
        host=input('已有 IvorySQL 本机地址 [127.0.0.1]: ').strip() or host
        port=int(input('PostgreSQL 协议端口 [5432]: ').strip() or '5432')
        adminuser=input('数据库管理员角色 [ivorysql]: ').strip() or adminuser
        adminpassword=getpass.getpass('管理员密码（不会保存，输入不显示）: ')
    if host not in ('127.0.0.1','localhost','::1') and not host.startswith('/'):
        raise SystemExit('此部署方案使用本机 IvorySQL。远程实例请按源码配置流程单独处理。')
    try:
        conn=psycopg.connect(host=host,port=port,user=adminuser,password=adminpassword,dbname='postgres',autocommit=True,connect_timeout=10)
    except psycopg.Error:
        raise SystemExit('无法连接数据库，未创建数据库。请确认本机实例、协议端口、管理员及认证方式。')
    with conn:
        version=conn.execute('SELECT version()').fetchone()[0]
        if 'ivorysql' not in version.lower():raise SystemExit('目标实例不是 IvorySQL，停止部署。')
        print('已确认目标实例为 IvorySQL。')
        dbname=name(input('新数据库名 [wechat_todo]: ').strip() or 'wechat_todo')
        role=name(input('新的专用数据库角色 [与数据库同名]: ').strip() or dbname)
        dbpassword=secrets.token_urlsafe(36)
        # Persist only the new app credential before DDL so a partial failure remains recoverable.
        private_file(target/'etc/app.pw',dbpassword+'\n',group)
        create_database(conn,dbname,role,dbpassword)
    config['db'].update(host=host,port=str(port),dbname=dbname,user=role,password_file=str(target/'etc/app.pw'))
    key=getpass.getpass('模型 API key（输入不显示）: ').strip()
    if not key:raise SystemExit('API key 不能为空。已创建的专用数据库保留，不自动删除。')
    url=input('模型地址 [https://api.deepseek.com]: ').strip() or 'https://api.deepseek.com'
    model=input('工具调用模型名（填写账号实际可用值）: ').strip()
    if not model:raise SystemExit('模型名称不能为空。')
    stylemodel=input('早报措辞模型名 [与工具调用模型相同]: ').strip() or model
    config['deepseek'].update(base_url=url,model=model,api_key_file=str(target/'etc/deepseek.key'))
    config['style']['model']=stylemodel
    config['weixin']['token_file']=str(target/'etc/weixin.token')
    config['logging']['file']=str(target/'log/bot.log')
    config['backup']={'pg_bin':str(pg_bin),'directory':'/var/backups/reminder-bot','keep_days':'14'}
    private_file(target/'etc/deepseek.key',key+'\n',group)
    asyncio.run(login_and_save(target,config,group))
    output=io.StringIO();config.write(output)
    private_file(target/'etc/config.ini',output.getvalue(),group)
    print('配置已保存到受保护的 etc/，未显示账号标识或密钥。')
    return config
