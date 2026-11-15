"""Non-blocking start-up backup and anonymous local timing diagnostics."""
from __future__ import annotations
import sqlite3, threading
from pathlib import Path
from datetime import datetime


def log_startup(path: Path, elapsed_ms: int):
    """Local-only timing; no user content, network calls or paths logged."""
    try:
        path=Path(path)
        with path.open('a',encoding='utf8') as out:
            out.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} | UI ready: {elapsed_ms} ms\n")
    except OSError:
        pass


def backup_snapshot(db_path: Path, backups: Path):
    """Online SQLite backup from fresh connections; safe with WAL and open UI DB."""
    db_path=Path(db_path);backups=Path(backups)
    if not db_path.is_file():return None
    backups.mkdir(parents=True,exist_ok=True)
    dest=backups/f"collection_{datetime.now():%Y%m%d_%H%M%S_%f}.sqlite3"
    try:
        with sqlite3.connect(f'file:{db_path.as_posix()}?mode=ro',uri=True,timeout=8) as source:
            with sqlite3.connect(dest) as target:
                source.backup(target,pages=128,sleep=0.05)
        files=sorted(backups.glob('collection_*.sqlite3'),reverse=True)
        for stale in files[12:]: stale.unlink(missing_ok=True)
        return dest
    except (OSError,sqlite3.Error):
        dest.unlink(missing_ok=True)
        return None


def backup_in_background(db_path: Path, backups: Path):
    worker=threading.Thread(target=backup_snapshot,args=(db_path,backups),daemon=True,name='lego-startup-backup')
    worker.start()
    return worker
