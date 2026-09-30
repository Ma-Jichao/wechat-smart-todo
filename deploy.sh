#!/usr/bin/env bash
# Native installation entry point. No Docker and no privilege escalation.
set -euo pipefail
cd "$(dirname "$0")"
PYTHON=''
for candidate in python3 python3.12 python3.11 python3.10; do
  if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3,10))' 2>/dev/null; then
    PYTHON=$(command -v "$candidate")
    break
  fi
done
if [ -z "$PYTHON" ]; then
  if [[ " $* " == *" --dry-run "* ]]; then
    echo '需要 Python 3.10+。实际安装时会先征得同意，再按系统包管理器安装。'
    exit 0
  fi
  [ "$(id -u)" = 0 ] || { echo '请以 root 运行；脚本不使用 sudo。'; exit 1; }
  if command -v apt-get >/dev/null; then
    . /etc/os-release
    case "${ID}:${VERSION_ID}" in
      ubuntu:22.04|ubuntu:24.04|debian:12|debian:13) packages=(python3 python3-venv); PYTHON=python3 ;;
      *) echo '此系统仓库未确认提供 Python 3.10+，请先安装后重试。'; exit 1 ;;
    esac
    echo "将通过 apt-get 安装：${packages[*]}"
    read -r -p '输入 INSTALL PYTHON 同意安装：' answer
    [ "$answer" = 'INSTALL PYTHON' ] || exit 1
    apt-get update
    apt-get install -y "${packages[@]}"
  elif command -v dnf >/dev/null; then
    echo '将通过 dnf 安装 python3.11。'
    read -r -p '输入 INSTALL PYTHON 同意安装：' answer
    [ "$answer" = 'INSTALL PYTHON' ] || exit 1
    dnf install -y python3.11
    PYTHON=python3.11
  else
    echo '无法自动安装受支持 Python，请先安装 Python 3.10+。'; exit 1
  fi
fi
exec "$PYTHON" scripts/install.py "$@"
