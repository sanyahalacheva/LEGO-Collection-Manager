"""Per-installation recent LEGO copies, stored next to the collection database."""
from __future__ import annotations
import json
from pathlib import Path

class RecentSets:
    def __init__(self,path: Path):
        self.path=Path(path)
        try:
            value=json.loads(self.path.read_text(encoding='utf8'))
            self.ids=list(dict.fromkeys(int(x) for x in value if int(x)>0))[:5] if isinstance(value,list) else []
        except (ValueError,TypeError,OSError):
            self.ids=[]
    def _write(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temp=self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.ids),encoding='utf8')
        temp.replace(self.path)
    def record(self,sid):
        sid=int(sid)
        if sid<=0: return
        self.ids=[sid]+[x for x in self.ids if x!=sid]
        self.ids=self.ids[:5]
        self._write()
    def forget(self,sid):
        self.ids=[x for x in self.ids if x!=int(sid)]
        self._write()
