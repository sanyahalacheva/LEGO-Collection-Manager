"""Dependency-free checks of legacy SQLite compatibility and update package safety.
Run from repository root: python -m unittest discover -s tests -v
"""
import ast
import datetime
import importlib.util
import os
import pathlib
import shutil
import sqlite3
import tempfile
import types
import unittest
import zipfile

ROOT=pathlib.Path(__file__).resolve().parents[1]


def load_store_class(db_file, backup_dir):
    tree=ast.parse((ROOT/'main.py').read_text(encoding='utf-8'))
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Store')
    module=ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[]))
    env={'sqlite3':sqlite3,'DB_PATH':db_file,'BACKUPS':backup_dir,'datetime':datetime.datetime}
    exec(compile(module,'main.py','exec'),env)
    return env['Store']


def migrate(db):
    # Execute the ACTUAL production migration method, not a duplicate test copy.
    tree=ast.parse((ROOT/'features21.py').read_text(encoding='utf-8'))
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Features21')
    method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_feature_migrate')
    module=ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[]))
    namespace={}
    exec(compile(module,'features21.py','exec'),namespace)
    namespace['_feature_migrate'](types.SimpleNamespace(db=db))


class Compatibility(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.base=pathlib.Path(self.tmp.name)
        self.backups=self.base/'backups';self.backups.mkdir()
        self.db_file=self.base/'collection.sqlite3'
        self.Store=load_store_class(self.db_file,self.backups)
        self.db=self.Store()
    def tearDown(self):
        self.db.db.close();self.tmp.cleanup()
    def test_duplicate_instances_and_safe_migration(self):
        inventory=[{'part':{'part_num':'3001','name':'Brick 2x4'},'color':{'id':4,'name':'Red'},'quantity':4,'part_img_url':''}]
        entry={'set_num':'1234-1','name':'Test set','year':2024,'set_img_url':''}
        first=self.db.add_set(entry,inventory);second=self.db.add_set(entry,inventory)
        self.db.start(first);self.db.set_owned(self.db.parts_for(first)[0]['id'],3)
        self.assertEqual(2,len(self.db.all_sets()))
        backup=self.base/'preserved.sqlite3';copy=sqlite3.connect(backup)
        self.db.db.backup(copy);copy.close()
        migrate(self.db);migrate(self.db)
        self.assertEqual(3,self.db.parts_for(first)[0]['owned'])
        self.assertEqual(0,self.db.parts_for(second)[0]['owned'])
        self.assertEqual(2,self.db.count('SELECT COUNT(*) FROM sets'))
        self.assertEqual(3,self.db.db.execute('PRAGMA user_version').fetchone()[0])
        self.db.delete_set(second)
        self.assertEqual(1,self.db.count('SELECT COUNT(*) FROM sets'))
        self.assertEqual(3,self.db.parts_for(first)[0]['owned'])
        self.assertTrue(backup.is_file())
        self.assertEqual(1,self.db.count("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='set_box_art'"))
    def test_loose_quantity_and_undo(self):
        item={'part_num':'3001','color_id':4,'name':'Brick 2x4','color':'Red','image':''}
        self.db.add_loose_quantity(item,5);self.db.add_loose_quantity(item,7)
        self.assertEqual(12,self.db.loose_rows()[0]['quantity'])
        self.db.undo_last()
        self.assertEqual(5,self.db.loose_rows()[0]['quantity'])
        self.db.set_loose(item,0)
        self.assertEqual([],self.db.loose_rows())
    def test_theme_rollup_prefers_lego_series(self):
        migrate(self.db)
        with self.db.db:
            self.db.db.execute('INSERT INTO themes(id,name,parent_id) VALUES(50,?,NULL)',('Town',))
            self.db.db.execute('INSERT INTO themes(id,name,parent_id) VALUES(52,?,50)',('City',))
            self.db.db.execute('INSERT INTO themes(id,name,parent_id) VALUES(53,?,52)',('Police',))
        tree=ast.parse((ROOT/'features21.py').read_text(encoding='utf-8'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Features21')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='feature_top_theme')
        module=ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[]))
        ns={};exec(compile(module,'features21.py','exec'),ns)
        result=ns['feature_top_theme'](types.SimpleNamespace(db=self.db),53)
        self.assertEqual('City',result)

    def test_update_archive_rejects_data(self):
        spec=importlib.util.spec_from_file_location('update_core',ROOT/'update_core.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        f=self.base/'bad.zip'
        with zipfile.ZipFile(f,'w') as z:
            z.writestr('data/collection.sqlite3','do not overwrite')
        with self.assertRaises(ValueError):module.safe_extract(f,self.base/'unpacked')

if __name__=='__main__': unittest.main()
