"""Test the actual inventory sort function without requiring a GUI environment."""
import ast
from pathlib import Path
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]
source=(ROOT/'main.py').read_text(encoding='utf-8')
tree=ast.parse(source)
functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('part_size_key','sort_inventory_parts')]
ns={'re':re}
exec(compile(ast.fix_missing_locations(ast.Module(body=functions,type_ignores=[])),str(ROOT/'main.py'),'exec'),ns)

class InventoryOrder(unittest.TestCase):
    def test_reserve_last_in_each_mode_and_preserve_data(self):
        parts=[
            dict(part_num='3010',name='Brick 1 x 4',color='Red',spare=1,owned=2,required=3),
            dict(part_num='3000',name='Brick 1 x 1',color='Blue',spare=0,owned=1,required=2),
            dict(part_num='3020',name='Plate 1 x 2',color='Blue',spare=1,owned=0,required=1),
            dict(part_num='3030',name='Plate 2 x 4',color='Red',spare=0,owned=4,required=4),
        ]
        for mode in ('Таблица','Карти','Книжка'):
            sorted_parts=ns['sort_inventory_parts'](parts,mode)
            self.assertEqual([False,False,True,True],[bool(x['spare']) for x in sorted_parts])
            self.assertEqual({(x['part_num'],x['owned']) for x in parts},
                             {(x['part_num'],x['owned']) for x in sorted_parts})
        self.assertEqual(['3000','3030','3020','3010'],[p['part_num'] for p in ns['sort_inventory_parts'](parts,'Книжка')])

if __name__=='__main__': unittest.main()
