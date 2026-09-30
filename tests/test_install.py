"""Installer consent and overwrite protection. No installation or network access."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'scripts'))
import install
import provision

class EmptyResult:
    def fetchone(self):return None

class FakeConnection:
    def __init__(self,exists=False):self.writes=[];self.exists=exists
    def execute(self,query,parameters=None):
        if isinstance(query,str) and query.startswith('SELECT'):
            class Result:
                def fetchone(inner):return (1,) if self.exists else None
            return Result()
        self.writes.append(query)
        return EmptyResult()

class InstallerTests(unittest.TestCase):
    def test_declined_database_creation_does_not_write(self):
        conn=FakeConnection()
        with patch('builtins.input',return_value='no'):
            with self.assertRaises(SystemExit):provision.create_database(conn,'test_new_db','test_new_role','test-only-password')
        self.assertEqual(conn.writes,[])

    def test_existing_database_is_not_modified(self):
        conn=FakeConnection(exists=True)
        with patch('builtins.input') as prompt:
            with self.assertRaises(SystemExit):provision.create_database(conn,'existing_db','existing_role','test-only-password')
        self.assertEqual(conn.writes,[]);prompt.assert_not_called()

    def test_consent_must_match_database_name(self):
        conn=FakeConnection()
        with patch('builtins.input',return_value='CREATE other_database'):
            with self.assertRaises(SystemExit):provision.create_database(conn,'test_new_db','test_new_role','test-only-password')
        self.assertEqual(conn.writes,[])

    def test_existing_install_directory_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);sentinel=root/'existing.txt';sentinel.write_text('existing-asset')
            with self.assertRaises(SystemExit):install.validate_target(root)
            self.assertEqual(sentinel.read_text(),'existing-asset')

    def test_declined_install_is_not_downloaded(self):
        with patch.object(install,'check_ports'),patch.object(install,'package_manager',return_value=('deb','apt-get')),patch.object(install.platform,'machine',return_value='x86_64'),patch('builtins.input',return_value='no'),patch.object(install.urllib.request,'urlopen') as download,patch.object(install.Path,'exists',return_value=False):
            with self.assertRaises(SystemExit):install.install_ivorysql({'ID':'ubuntu'})
        download.assert_not_called()

    def test_production_config_rejected_before_secret_read(self):
        sys.path.insert(0,str(ROOT))
        from support import load_test_config
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.ini'
            path.write_text('[db]\ndbname=reminder\npassword_file=/not/a/test/credential\n')
            with patch.dict('os.environ',{'REMINDER_TEST_CONFIG':str(path)}),patch('support.load') as read_secret:
                with self.assertRaises(SystemExit):load_test_config()
            read_secret.assert_not_called()

    def test_identifier_injection_rejected(self):
        for value in ('foo; DROP DATABASE test','bad-name','1bad','a'*64):
            with self.assertRaises(SystemExit):provision.name(value)

if __name__=='__main__':unittest.main()
