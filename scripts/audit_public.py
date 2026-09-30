"""Scan publishable files without printing matched secrets. Run before every commit."""
from pathlib import Path
import ipaddress
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = {
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'API token': re.compile(r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16})'),
    'phone number': re.compile(r'(?<![A-Za-z0-9])1[3-9][0-9]{9}(?![A-Za-z0-9])'),
    'personal home path': re.compile(r'/(?:Users|home)/[A-Za-z][^/\s]+/'),
    'email': re.compile(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b'),
}
ALLOWED_EMAILS = {'test-user@im.wechat', 'someone-else@im.wechat'}
BLOCKED_PARTS = {'etc', 'log', 'backups', 'hermes-backup', 'offsite-backup', '.venv', 'venv', '__pycache__'}
BLOCKED_SUFFIXES = {'.pw', '.key', '.pem', '.token', '.dump', '.zip', '.gz', '.pyc', '.log', '.sqlite', '.sqlite3', '.p12', '.pfx'}

def scan(paths, indexed=False):
    findings = []
    for path in paths:
        rel = path.relative_to(ROOT)
        if path.is_symlink():
            findings.append((rel, 0, 'symlink')); continue
        if set(rel.parts) & BLOCKED_PARTS or path.suffix in BLOCKED_SUFFIXES or path.name.startswith('.env'):
            findings.append((rel, 0, 'private file')); continue
        try:
            content = (subprocess.check_output(["git", "show", ":" + rel.as_posix()], cwd=ROOT).decode()
                       if indexed else path.read_text())
        except (UnicodeError, OSError):
            findings.append((rel, 0, 'unreviewed binary')); continue
        for n, line in enumerate(content.splitlines(), 1):
            for label, regex in PATTERNS.items():
                for match in regex.finditer(line):
                    if label == 'email' and match.group() in ALLOWED_EMAILS:
                        continue
                    findings.append((rel, n, label))
            for value in re.findall(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])', line):
                try:
                    ip = ipaddress.ip_address(value)
                    if ip.is_global:
                        findings.append((rel, n, 'public IP'))
                except ValueError:
                    pass
    return findings

def main():
    if (ROOT/'.git').exists():
        names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
        paths = [ROOT/name for name in names if name]
        if not paths:
            raise SystemExit('No indexed files; stage the explicit publishable file list first.')
    else:
        paths = [p for p in ROOT.rglob('*') if p.is_file() and '.git' not in p.parts]
    findings = scan(paths, indexed=(ROOT/".git").exists())
    for path, line, label in findings:
        print(f'{path}:{line}: {label} (value hidden)')
    print(f'Scanned {len(paths)} files; {len(findings)} findings. Manual review still required.')
    return bool(findings)

if __name__ == '__main__':
    sys.exit(main())
