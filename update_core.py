"""Conservative portable update functions. No user data is copied into update ZIPs."""
from __future__ import annotations
import hashlib, json, os, re, shutil, sqlite3, time, zipfile
from pathlib import Path

RELEASE_ZIP='LEGO-Collection-Manager-UPDATE.zip'
FULL_ZIP='LEGO-Collection-Manager-PORTABLE.zip'
HASH_NAME=RELEASE_ZIP+'.sha256'
FILES={'LEGO Collection Manager.exe', 'lego_icon.ico', 'collector_icon.ico', 'version.json'}
DIRS={'_internal'}
MAX_ZIP=600*1024*1024
MAX_EXTRACTED=1_200*1024*1024


def parse_version(s):
    match=re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', str(s).strip())
    if not match: raise ValueError('Невалидна версия: '+str(s))
    return tuple(int(x) for x in match.groups())


def read_version(root):
    try: return json.loads((Path(root)/'version.json').read_text(encoding='utf8'))['version']
    except (OSError,KeyError,ValueError): return '2.0.0'


def safe_extract(archive, destination):
    """Strict allowlist; no symlinks, absolute paths, traversal or user data."""
    destination=Path(destination)
    with zipfile.ZipFile(archive) as z:
        names=z.infolist()
        if len(names)>8000: raise ValueError('Архивът съдържа твърде много файлове.')
        if sum(x.file_size for x in names)>MAX_EXTRACTED: raise ValueError('Архивът е прекалено голям.')
        for info in names:
            name=info.filename.replace('\\','/')
            chunks=name.rstrip('/').split('/')
            if not name or name.startswith('/') or any(x in ('','.', '..') for x in chunks):
                raise ValueError('Невалиден път в архива.')
            if len(chunks[0])>=2 and chunks[0][1]==':':raise ValueError('Абсолютен път в архива.')
            if chunks[0] not in FILES|DIRS: raise ValueError('Непозволен файл в обновлението: '+name)
            if len(chunks)>1 and chunks[0] != '_internal':raise ValueError('Невалиден път в архива.')
            mode=(info.external_attr>>16)&0o170000
            if mode==0o120000:raise ValueError('Символни връзки не са разрешени.')
        z.extractall(destination)
    if not (destination/'LEGO Collection Manager.exe').is_file():raise ValueError('Липсва EXE в архива.')
    if not (destination/'_internal').is_dir():raise ValueError('Липсват библиотеките в архива.')
    try:parse_version(json.loads((destination/'version.json').read_text(encoding='utf8'))['version'])
    except Exception as e:raise ValueError('Липсва валиден version.json') from e


def checksum(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def verify_sha(path,sha_file):
    actual=checksum(path)
    expected=Path(sha_file).read_text(encoding='ascii').split()[0].lower()
    if not re.fullmatch('[0-9a-f]{64}',expected) or actual!=expected:
        raise ValueError('SHA-256 проверката не е успешна. Обновлението е отказано.')


def db_backup(root):
    root=Path(root)
    original=root/'data'/'collection.sqlite3'
    if not original.is_file():return
    backups=root/'data'/'backups';backups.mkdir(parents=True,exist_ok=True)
    dest=backups/('before_update_'+time.strftime('%Y%m%d_%H%M%S')+'.sqlite3')
    source=sqlite3.connect(f'file:{original.as_posix()}?mode=ro',uri=True)
    target=sqlite3.connect(dest)
    try:source.backup(target)
    finally:source.close();target.close()


def replace_app(root,staged,progress=None):
    """Backup and replace program files; never touch data, updater, updates.json."""
    root=Path(root).resolve();staged=Path(staged).resolve()
    safe={'LEGO Collection Manager.exe','_internal','lego_icon.ico','collector_icon.ico','version.json'}
    last_backup=root/'.previous_program'
    temp_backup=root/'.update_rollback'
    if temp_backup.exists():shutil.rmtree(temp_backup)
    temp_backup.mkdir()
    moved_old=[];moved_new=[]
    try:
        if progress:progress('Създавам резервно копие на базата…')
        db_backup(root)
        # previous program is retained until next successful update.
        if progress:progress('Заменям програмните файлове…')
        for name in safe:
            path=root/name
            if path.exists():
                os.replace(path,temp_backup/name)
                moved_old.append(name)
        for name in safe:
            path=staged/name
            if path.exists():
                os.replace(path,root/name)
                moved_new.append(name)
        if last_backup.exists():shutil.rmtree(last_backup)
        os.replace(temp_backup,last_backup)
    except Exception:
        for name in reversed(moved_new):
            path=root/name
            if path.is_dir():shutil.rmtree(path)
            elif path.exists():path.unlink()
        for name in moved_old:
            path=temp_backup/name
            if path.exists():os.replace(path,root/name)
        raise


def repo_name(root):
    path=Path(root)/'updates.json'
    try:repo=json.loads(path.read_text(encoding='utf8')).get('repo','').strip()
    except (OSError,ValueError):repo=''
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo) or 'YOUR_' in repo.upper():
        raise ValueError('Посочи GitHub хранилището в updates.json (например user/LEGO-Collection-Manager).')
    return repo


def github_latest(root,timeout=12):
    import urllib.request
    repo=repo_name(root)
    req=urllib.request.Request('https://api.github.com/repos/'+repo+'/releases/latest',
        headers={'Accept':'application/vnd.github+json','User-Agent':'LEGO-Collection-Manager-Updater'})
    with urllib.request.urlopen(req,timeout=timeout) as r:
        response=json.load(r)
    version=str(response.get('tag_name','')).lstrip('v')
    parse_version(version)
    urls={a['name']:a['browser_download_url'] for a in response.get('assets',[]) if 'name' in a and 'browser_download_url' in a}
    if RELEASE_ZIP not in urls or HASH_NAME not in urls:
        raise ValueError('В последното GitHub Release липсват ZIP или SHA256.')
    prefix='https://github.com/'+repo+'/releases/download/'
    for name in (RELEASE_ZIP,HASH_NAME):
        if not urls[name].startswith(prefix) or not urls[name].startswith('https://'):
            raise ValueError('Непозволен адрес за изтегляне.')
    return version,urls,response.get('html_url','')


def download(url,dest,max_size=MAX_ZIP):
    import urllib.request
    req=urllib.request.Request(url,headers={'User-Agent':'LEGO-Collection-Manager-Updater'})
    with urllib.request.urlopen(req,timeout=30) as response,open(dest,'wb') as f:
        total=0
        while True:
            part=response.read(1024*1024)
            if not part:break
            total+=len(part)
            if total>max_size:raise ValueError('Файлът е твърде голям.')
            f.write(part)
