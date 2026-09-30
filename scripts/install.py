"""Native IvorySQL + venv + systemd installer; database operations require consent."""
from __future__ import annotations
import argparse
import configparser
import getpass
import glob
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request

SOURCE = Path(__file__).resolve().parent.parent
PG_VERSION = '5.6'
SYSTEMD_DIR = Path('/etc/systemd/system')
UNIT_SEARCH_DIRS = (SYSTEMD_DIR, Path('/usr/lib/systemd/system'), Path('/lib/systemd/system'))
ASSETS = {
    ('deb','x86_64'): ('IvorySQL-5.6-74c405c-20260917.amd64.deb','82a2ab4d341f0b0214ce7c642f2f0475ac74670f6a9e291f26c8488b0b99aa56'),
    ('deb','aarch64'): ('IvorySQL-5.6-74c405c-20260914.arm64.deb','400e4ac81d882376d6a148c51e93b56d023843e74864044befbd27729564474e'),
    ('rpm','x86_64'): ('IvorySQL-5.6-74c405c-20260917.x86_64.rpm','fc1a586b4a77a7a8f64b8e1efb7f674ccd70915dfbb26c24745c2f907d1c44b2'),
    ('rpm','aarch64'): ('IvorySQL-5.6-74c405c-20260914.aarch64.rpm','aea51560c6433a7783785697f4941150206b1539ab1afd23006c7bebd7022562'),
}

def run(args, **kwargs):
    return subprocess.run([str(a) for a in args],check=True,**kwargs)

def confirm(phrase, description):
    print(description)
    if input(f'输入 {phrase} 同意；其他输入退出：').strip() != phrase:
        raise SystemExit('未获同意，已停止。')

def os_info():
    values={}
    if Path('/etc/os-release').exists():
        for line in Path('/etc/os-release').read_text().splitlines():
            if '=' in line:
                key,value=line.split('=',1);values[key]=value.strip('"\'')
    return values

def native_bins(explicit=None):
    if explicit:
        path=Path(explicit).resolve()
        if not (path/'postgres').is_file():raise SystemExit('指定的数据库 bin 路径无效。')
        return path
    candidates=[Path(p) for pattern in ('/usr/ivory-*/bin','/usr/local/ivorysql*/bin','/var/local/ivorysql/ivorysql-*/bin') for p in glob.glob(pattern)]
    # Discover custom native installs from the running database binary.
    for process in Path('/proc').glob('[0-9]*') if Path('/proc').exists() else []:
        try:
            executable=(process/'exe').resolve(strict=True)
            if executable.name=='postgres' and 'ivory' in str(executable).lower():
                candidates.append(executable.parent)
        except (OSError,RuntimeError):pass
    candidates=[p for p in candidates if (p/'postgres').is_file()]
    if len(set(candidates))>1:
        raise SystemExit('检测到多个 IvorySQL 安装，请用 --pg-bin 指定目标实例的 bin 目录。')
    return candidates[0] if candidates else None

def package_manager():
    if shutil.which('apt-get'):return 'deb','apt-get'
    if shutil.which('dnf'):return 'rpm','dnf'
    if shutil.which('yum'):return 'rpm','yum'
    raise SystemExit('未识别包管理器，已有实例可用 --pg-bin；否则请按官方文档安装 IvorySQL。')

def validate_target(target):
    if not target.is_absolute() or any(c in str(target) for c in '\n\r\t %'):
        raise SystemExit('安装路径须为绝对路径，且不能包含空白或百分号。')
    if target.exists() and any(target.iterdir()):
        raise SystemExit('安装目录非空，拒绝覆盖。升级现有安装请走人工审阅流程。')
    for name in ('reminder-bot.service','reminder-backup.service','reminder-backup.timer'):
        if any((base/name).exists() for base in UNIT_SEARCH_DIRS):
            raise SystemExit(f'{name} 已存在，拒绝覆盖已有服务。')

def check_ports():
    for port in (5432,1521):
        with socket.socket() as sock:
            try:sock.bind(('127.0.0.1',port))
            except OSError:raise SystemExit(f'端口 {port} 已占用。请定位已有数据库，不会另行安装或停掉已有服务。')

def ensure_user(name):
    try:return pwd.getpwnam(name)
    except KeyError:
        run(['useradd','--system','--user-group','--no-create-home','--shell','/usr/sbin/nologin',name])
        return pwd.getpwnam(name)

def private_file(path,value,group=None):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:stream.write(value)
    if group is not None:
        os.chown(path,0,group);path.chmod(0o640)

def install_ivorysql(info):
    check_ports()
    data=Path('/var/lib/ivorysql/data')
    unit=Path('/etc/systemd/system/ivorysql.service')
    if unit.exists() or (data.exists() and any(data.iterdir())):
        raise SystemExit('发现已有 IvorySQL 服务或数据目录；请人工确认实例位置，不会初始化覆盖。')
    kind,manager=package_manager()
    if kind=='deb' and info.get('ID') not in ('ubuntu','debian'):
        raise SystemExit('该 deb 系统尚未确认原生安装兼容性，请先按官方文档安装。')
    arch=platform.machine().lower();arch={'amd64':'x86_64','arm64':'aarch64'}.get(arch,arch)
    if (kind,arch) not in ASSETS:raise SystemExit('该 CPU 架构暂无锁定的官方安装包，请先手动安装 IvorySQL。')
    name,digest=ASSETS[(kind,arch)]
    confirm('INSTALL IVORYSQL',f'未发现原生实例。将安装官方 IvorySQL {PG_VERSION}（{kind}/{arch}），创建 {data} 和 ivorysql.service，端口仅监听本机。不创建应用数据库，稍后另行征求同意。')
    with tempfile.TemporaryDirectory(prefix='ivorysql-install-') as tmp:
        package=Path(tmp)/name
        url=f'https://github.com/IvorySQL/IvorySQL/releases/download/IvorySQL_5.6/{name}'
        with urllib.request.urlopen(url,timeout=90) as response,package.open('wb') as output:
            shutil.copyfileobj(response,output)
        checksum=hashlib.sha256()
        with package.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):checksum.update(block)
        if checksum.hexdigest()!=digest:
            raise SystemExit('安装包 SHA256 与锁定的官方发布校验值不一致，停止安装。')
        if kind=='deb':run(['dpkg','-i',package])
        else:run([manager,'install','-y',package])
    binary=Path('/usr/ivory-5/bin')
    if not (binary/'initdb').exists():raise SystemExit('官方安装后的路径与预期不一致，停止初始化。')
    owner=ensure_user('ivorysql')
    data.mkdir(parents=True,exist_ok=True);os.chown(data,owner.pw_uid,owner.pw_gid);data.chmod(0o700)
    password=secrets.token_urlsafe(36)
    secret=Path('/root/.ivorysql/superuser.pw')
    if secret.exists():raise SystemExit('超级用户口令文件已存在，拒绝覆盖。')
    secret.parent.mkdir(exist_ok=True,mode=0o700)
    private_file(secret,password+'\n')
    # The password is never passed in argv or printed. initdb reads a temporary file.
    with tempfile.TemporaryDirectory(prefix='ivory-init-') as tmp:
        os.chown(tmp,owner.pw_uid,owner.pw_gid)
        pwfile=Path(tmp)/'password'
        private_file(pwfile,password+'\n');os.chown(pwfile,owner.pw_uid,owner.pw_gid)
        run(['runuser','-u','ivorysql','--',binary/'initdb','-D',data,'-U','ivorysql','--auth-local=peer','--auth-host=scram-sha-256','--pwfile',pwfile])
    with (data/'postgresql.conf').open('a') as f:
        f.write("\nlisten_addresses = '127.0.0.1'\nport = 5432\nivorysql.listen_addresses = '127.0.0.1'\nivorysql.port = 1521\nunix_socket_directories = '/run/ivorysql'\n")
    unit.write_text(f'''[Unit]
Description=IvorySQL 5.6 database server
After=network-online.target
Wants=network-online.target
[Service]
Type=notify
User=ivorysql
Group=ivorysql
RuntimeDirectory=ivorysql
RuntimeDirectoryMode=0750
ExecStart={binary}/postgres -D {data}
ExecReload=/bin/kill -HUP $MAINPID
KillMode=mixed
KillSignal=SIGINT
TimeoutStopSec=300
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
''')
    run(['systemctl','daemon-reload']);run(['systemctl','enable','--now','ivorysql.service'])
    return binary,password

def ensure_venv(python, target):
    result=subprocess.run([str(python),'-m','venv',str(target)],capture_output=True,text=True)
    if result.returncode==0:return
    kind,manager=package_manager()
    version=f'{sys.version_info.major}.{sys.version_info.minor}'
    package=f'python{version}-venv' if kind=='deb' else f'python{version}'
    confirm('INSTALL VENV',f'系统缺少可用 venv/ensurepip，将通过 {manager} 安装 {package}。')
    if kind=='deb':run([manager,'update'])
    run([manager,'install','-y',package])
    run([str(python),'-m','venv',str(target)])


def database_service():
    result=subprocess.run(['systemctl','show','ivorysql.service','-p','LoadState','--value'],capture_output=True,text=True)
    if result.returncode==0 and result.stdout.strip()=='loaded':return 'ivorysql.service'
    value=input('已有数据库的 systemd 服务名（留空则不设置服务依赖）: ').strip()
    if value and not __import__('re').fullmatch(r'[A-Za-z0-9_.@-]+\.service',value):
        raise SystemExit('服务名格式无效。')
    return value


def provision_app(target,pg_bin,fresh_admin_password):
    from importlib.util import spec_from_file_location,module_from_spec
    spec=spec_from_file_location('provision',SOURCE/'scripts/provision.py')
    module=module_from_spec(spec);spec.loader.exec_module(module)
    return module.configure(target,pg_bin,fresh_admin_password)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run',action='store_true',help='只显示检测和计划，不安装、不连接数据库、不创建文件')
    parser.add_argument('--install-dir',type=Path,default=Path('/opt/reminder-bot'))
    parser.add_argument('--pg-bin',type=Path,help='已有原生 IvorySQL bin 目录')
    parser.add_argument('--prepared',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    info=os_info();binary=native_bins(args.pg_bin)
    print(f'系统：{info.get("PRETTY_NAME",platform.system())}；CPU：{platform.machine()}')
    print('数据库路径：'+(str(binary) if binary else '未检测到；实际安装时先征求同意'))
    print(f'计划：原生 IvorySQL → 同意后新建应用数据库 → Python venv → 扫码 → systemd；应用目录 {args.install_dir}')
    if args.dry_run:return 0
    if platform.system()!='Linux' or not Path('/run/systemd/system').exists():
        raise SystemExit('自动部署仅用于运行 systemd 的 Linux；其他环境请使用源码开发流程。')
    if os.geteuid()!=0:raise SystemExit('请以 root 运行；脚本不调用 sudo。')
    if not sys.stdin.isatty():raise SystemExit('安装需终端交互同意、密钥输入和扫码，不支持管道自动确认。')
    validate_target(args.install_dir)
    if not args.prepared:
        print(f'将安装 Python 项目依赖、创建 reminderbot 用户和 {args.install_dir}，注册应用 systemd 服务。数据库安装和建库将另行征求同意。')
        # A temporary venv supplies installer dependencies; real app venv is created below.
        with tempfile.TemporaryDirectory(prefix='reminder-installer-') as tmp:
            ensure_venv(sys.executable,tmp)
            py=Path(tmp)/'bin/python'
            run([py,'-m','pip','install','-r',SOURCE/'requirements.txt'])
            run([py,Path(__file__),*sys.argv[1:],'--prepared'])
        return 0
    admin_password=None
    if binary is None:binary,admin_password=install_ivorysql(info)
    db_service='ivorysql.service' if admin_password is not None else database_service()
    owner=ensure_user('reminderbot')
    if owner.pw_uid==0:raise SystemExit('reminderbot 不能使用 root UID。')
    args.install_dir.mkdir(parents=True,exist_ok=True)
    args.install_dir.chmod(0o755)
    for folder in ('app','migrations','scripts','deploy','docs'):
        shutil.copytree(SOURCE/folder,args.install_dir/folder,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in ('requirements.txt','config.example.ini','README.md','AGENTS.md'):
        shutil.copy2(SOURCE/name,args.install_dir/name)
    for folder in ('etc','log'):
        directory=args.install_dir/folder;directory.mkdir();directory.chmod(0o750);os.chown(directory,0 if folder=='etc' else owner.pw_uid,owner.pw_gid)
    provision_app(args.install_dir,binary,admin_password)
    py=args.install_dir/'venv/bin/python'
    ensure_venv(sys.executable,args.install_dir/'venv')
    run([py,'-m','pip','install','-r',args.install_dir/'requirements.txt'])
    run(['runuser','-u','reminderbot','--',py,args.install_dir/'scripts/migrate.py','--config',args.install_dir/'etc/config.ini'])
    run(['runuser','-u','reminderbot','--',py,'-m','app.main','--check','--config',args.install_dir/'etc/config.ini'],cwd=args.install_dir)
    for name in ('reminder-bot.service','reminder-backup.service','reminder-backup.timer'):
        content=(SOURCE/'deploy'/name).read_text().replace('/opt/reminder-bot',str(args.install_dir))
        if db_service:
            content=content.replace('ivorysql.service',db_service)
        else:
            content=content.replace(' ivorysql.service','').replace('After=ivorysql.service\n','').replace('Requires=ivorysql.service\n','')
        (SYSTEMD_DIR/name).write_text(content)
    run(['systemctl','daemon-reload']);run(['systemctl','enable','--now','reminder-bot.service','reminder-backup.timer'])
    print('原生部署完成。请在微信主动发一句话建立会话，再创建提醒并确认到点实收。')
    return 0

if __name__=='__main__':
    try:sys.exit(main())
    except (subprocess.CalledProcessError,KeyboardInterrupt):
        raise SystemExit('安装中断。已创建资产予以保留，未回滚删除任何数据库；修复前检查当前安装目录与服务状态。')
