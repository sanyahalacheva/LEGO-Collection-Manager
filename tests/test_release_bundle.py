import tempfile, pathlib, zipfile, json, unittest
from update_core import safe_extract
class TestReleaseBundle(unittest.TestCase):
    def test_collector_icon_allowlisted(self):
        with tempfile.TemporaryDirectory() as td:
            base=pathlib.Path(td)
            archive=base/'release.zip'
            with zipfile.ZipFile(archive,'w') as z:
                z.writestr('LEGO Collection Manager.exe',b'exe')
                z.writestr('_internal/library.dll',b'lib')
                z.writestr('collector_icon.ico',b'ico')
                z.writestr('version.json',json.dumps({'version':'2.1.7'}))
            safe_extract(archive,base/'stage')
            self.assertTrue((base/'stage'/'collector_icon.ico').exists())
    def test_never_allow_data(self):
        with tempfile.TemporaryDirectory() as td:
            base=pathlib.Path(td)
            with zipfile.ZipFile(base/'bad.zip','w') as z:
                z.writestr('data/collection.sqlite3',b'fake')
            with self.assertRaises(ValueError):safe_extract(base/'bad.zip',base/'stage')
if __name__=='__main__':unittest.main()
