"""Verify production sorting code and live data changes without launching Qt."""
import ast
from pathlib import Path
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]
tree=ast.parse((ROOT/'main.py').read_text(encoding='utf-8'))
funcs=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('part_size_key','sort_missing_parts')]
ns={'re':re}
exec(compile(ast.fix_missing_locations(ast.Module(body=funcs,type_ignores=[])),str(ROOT/'main.py'),'exec'),ns)
sort_missing_parts=ns['sort_missing_parts']

class MissingOrder(unittest.TestCase):
    def test_color_then_size_then_number_and_unknown_last(self):
        entries=[
            dict(part_num='ZZ',color_id=7,color='Red',name='Special Clip',missing=1),
            dict(part_num='B',color_id=4,color='Blue',name='Plate 2 x 6',missing=3),
            dict(part_num='R4',color_id=7,color='Red',name='Brick 2 x 4',missing=12),
            dict(part_num='R2',color_id=7,color='Red',name='Plate 1 x 2',missing=2),
            dict(part_num='A',color_id=4,color='Blue',name='Plate 1 x 1',missing=9),
            dict(part_num='R3',color_id=7,color='Red',name='Plate 1 x 2',missing=1),
        ]
        snapshot=[dict(p) for p in entries]
        out=sort_missing_parts(entries)
        self.assertEqual(['A','B','R2','R3','R4','ZZ'],[x['part_num'] for x in out])
        self.assertEqual(snapshot,entries)  # input untouched
        self.assertEqual(12,next(x['missing'] for x in out if x['part_num']=='R4'))

    def test_new_missing_item_inserts_at_proper_location(self):
        entries=[
            dict(part_num='3030',color_id=2,color='Red',name='Plate 2 x 6'),
            dict(part_num='3010',color_id=2,color='Red',name='Plate 1 x 2'),
        ]
        self.assertEqual(['3010','3030'],[p['part_num'] for p in sort_missing_parts(entries)])
        entries.append(dict(part_num='3020',color_id=2,color='Red',name='Plate 2 x 2'))
        self.assertEqual(['3010','3020','3030'],[p['part_num'] for p in sort_missing_parts(entries)])
        entries.pop(1)
        self.assertEqual(['3020','3030'],[p['part_num'] for p in sort_missing_parts(entries)])

if __name__=='__main__': unittest.main()
