"""LEGO Collection Manager — offline-first desktop manager (Bulgarian UI)."""
from __future__ import annotations
import json, os, sys, sqlite3, shutil, time, html, re, hashlib, subprocess, uuid, threading
from collections import OrderedDict
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, quote
import requests
from PySide6.QtCore import QEvent, Qt, QByteArray, QObject, Signal, QRunnable, QThreadPool, QAbstractTableModel, QModelIndex, QSize, QTimer, QPoint, QRect
from PySide6.QtGui import QPixmap, QIcon, QPainter, QColor, QFont, QDesktopServices, QPageSize, QTextDocument, QPen, QImage
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QStackedWidget, QSplitter, QTableView, QHeaderView, QMessageBox, QDialog, QDialogButtonBox, QSpinBox, QScrollArea,
    QGridLayout, QComboBox, QFrame, QFileDialog, QTabWidget, QCheckBox, QMenu, QSizePolicy, QFormLayout, QGroupBox,
    QAbstractItemView, QSlider, QInputDialog, QProgressBar, QStyledItemDelegate, QStyle, QScroller, QScrollerProperties, QAbstractScrollArea)
from PySide6.QtPrintSupport import QPrinter, QPrintDialog
from PySide6.QtSvg import QSvgRenderer
from features21 import Features21
from usage_metrics import UsageMetrics

# Portable storage: next to main.py in development, next to .exe when frozen.
PROGRAM_DIR = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
APP_DIR = PROGRAM_DIR / 'data'
IMAGE_DIR = APP_DIR / 'images'
BACKUPS = APP_DIR / 'backups'
for p in [APP_DIR, IMAGE_DIR, BACKUPS]: p.mkdir(parents=True, exist_ok=True)
DB_PATH = APP_DIR / 'collection.sqlite3'
SETTINGS_PATH = APP_DIR / 'settings.json'
API_BASE = 'https://rebrickable.com/api/v3/lego/'

def image_cache_path(url):
    suffix = '.png' if urlparse(url).path.lower().endswith('.png') else '.jpg'
    return IMAGE_DIR / (hashlib.sha256(url.encode('utf-8')).hexdigest() + suffix)

APP_ID = 'OpenAI.LEGOCollectionManager'
# Images on disk retain enough detail for the large preview, without keeping
# multi-megapixel originals. This does not change existing cached files.
MAX_IMAGE_SIDE = 760
MAX_MEMORY_CACHE_BYTES = 42 * 1024 * 1024


def cache_image_bytes(url, content):
    """Save a bounded, decoded image atomically. Called by worker threads."""
    if not content or len(content) > 6_000_000:
        raise ValueError('Invalid image size')
    if not (content.startswith(b'\x89PNG\r\n\x1a\n') or content.startswith(b'\xff\xd8\xff')
            or content.startswith(b'RIFF') or content.startswith(b'GIF8')):
        raise ValueError('Invalid image format')
    from PySide6.QtCore import Qt as ImageQt
    image = QImage.fromData(content)
    if image.isNull():
        raise ValueError('Неразпозната снимка')
    if max(image.width(), image.height()) > MAX_IMAGE_SIDE:
        image = image.scaled(MAX_IMAGE_SIDE, MAX_IMAGE_SIDE,
                             ImageQt.KeepAspectRatio, ImageQt.SmoothTransformation)
    path = image_cache_path(url)
    # Preserve the image file's suffix and format: QPixmap can load it normally.
    fmt = 'PNG' if path.suffix.lower() == '.png' else 'JPEG'
    if fmt == 'JPEG' and image.hasAlphaChannel():
        # Many LEGO assets use a transparent background; preserve white backdrop.
        canvas = QImage(image.size(), QImage.Format_RGB32)
        canvas.fill(ImageQt.white)
        painter = QPainter(canvas)
        painter.drawImage(0, 0, image)
        painter.end()
        image = canvas
    tmp = path.with_name(path.name + '.download_tmp')
    try:
        if not image.save(str(tmp), fmt, 82 if fmt == 'JPEG' else -1):
            raise ValueError('Неуспешно записване на снимката')
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def enable_touch_scroll(widget):
    """Enable finger swiping without intercepting mouse clicks on +/- controls."""
    viewport = widget.viewport() if isinstance(widget, QAbstractScrollArea) else widget
    try:
        QScroller.grabGesture(viewport, QScroller.ScrollerGestureType.TouchGesture)
        props=QScroller.scroller(viewport).scrollerProperties()
        props.setScrollMetric(QScrollerProperties.DecelerationFactor,0.13)
        props.setScrollMetric(QScrollerProperties.MaximumVelocity,0.75)
        QScroller.scroller(viewport).setScrollerProperties(props)
    except (TypeError, AttributeError):
        pass  # Qt platform without kinetic scroller support still has ordinary scrollbars.
    if isinstance(widget, QAbstractScrollArea):
        widget.verticalScrollBar().setSingleStep(23)
        widget.horizontalScrollBar().setSingleStep(23)
    if isinstance(widget, QAbstractItemView):
        widget.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        widget.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)


def compact_action(button, width=None):
    button.setMinimumHeight(31)
    button.setMaximumHeight(36)
    if width: button.setFixedWidth(width)
    return button


def part_size_key(part):
    """Approximate LEGO inventory size from visible dimensions; stable fallback for unspecified parts."""
    name=str(part.get('name',''))
    match=re.search(r'(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)(?:\s*[x×]\s*(\d+(?:\.\d+)?))?',name,re.I)
    if match:
        dim=[float(x) for x in match.groups() if x is not None]
        return (0,dim[0]*dim[1]*(dim[2] if len(dim)>2 else 1),max(dim),name.casefold())
    return (1,0,0,name.casefold())



def sort_missing_parts(parts):
    """Order aggregated missing parts by color, approximate physical footprint, number.

    Dimension inference is deliberately conservative: parts without explicit
    dimensions sort after sized pieces within their own color. Rows/data are not
    modified. Sorting the filtered snapshot means newly missing parts naturally
    appear in their correct location on refresh.
    """
    return sorted(parts, key=lambda p: (
        str(p['color'] or '').casefold(),
        part_size_key(p),
        str(p['part_num']).casefold(),
        int(p['color_id']),
    ))


def sort_inventory_parts(parts, mode):
    """Spare inventory must always appear after regular and alternate parts."""
    if mode == 'Книжка':
        return sorted(parts, key=lambda p: (bool(p['spare']), p['color'].casefold(),
                                            part_size_key(p), str(p['part_num'])))
    # Keep original relative ordering within each of the two groups.
    return sorted(parts, key=lambda p: bool(p['spare']))


CSS = """
/* LEGO Collection Manager — Collector Edition UI */
QWidget {font-family:'Segoe UI',Arial;font-size:13px;color:#243241;}
QMainWindow, QWidget#root {background:#F5F6F8;}
QLabel#heading {font-size:23px;font-weight:700;color:#202A35;}
QLabel#muted {color:#778393;}
QFrame#panel {background:#FFFFFF;border:1px solid #E4E8ED;border-radius:16px;}
QFrame#legoCard {background:#FFFFFF;border:1px solid #E1E5EA;border-radius:16px;}
QFrame#imageStage {background:#FAFBFC;border:1px solid #EFF0F2;border-radius:12px;}
QFrame#seriesHeader {background:transparent;border:none;}
QLabel#seriesTitle {font-size:19px;font-weight:750;color:#222D3A;}
QLabel#cardTitle {font-size:13px;font-weight:700;color:#202B36;}
QLabel#statusNew {background:#F0F2F5;color:#687383;border:1px solid #DFE4EA;border-radius:9px;padding:7px 11px;font-weight:700;}
QLabel#statusWork {background:#FFF5DA;color:#8C6317;border:1px solid #F5DFA5;border-radius:9px;padding:7px 11px;font-weight:700;}
QLabel#statusDone {background:#E5F4EC;color:#207750;border:1px solid #B6DFC9;border-radius:9px;padding:7px 11px;font-weight:700;}
QLabel#statusCount {color:#738092;font-size:11px;}
QProgressBar {background:#EDF0F3;border:none;border-radius:5px;min-height:8px;max-height:8px;text-align:center;}
QProgressBar::chunk {background:#D72735;border-radius:5px;}
QProgressBar#progressNew::chunk {background:#B9C2CD;}
QProgressBar#progressWork::chunk {background:#E7B940;}
QProgressBar#progressDone::chunk {background:#31A271;}
QPushButton {background:#FFFFFF;border:1px solid #DDE3E9;border-radius:10px;padding:6px 11px;font-weight:600;color:#293746;}
QPushButton:hover {background:#F7F8FA;border-color:#B9C5D1;}
QPushButton:pressed {background:#EBEEF2;}
QPushButton:disabled {background:#F4F5F6;border-color:#ECEEF0;color:#A4AEB9;}
QPushButton#primary {background:#D82230;border:1px solid #D82230;color:#FFFFFF;}
QPushButton#primary:hover {background:#B91D2C;border-color:#B91D2C;}
QPushButton#danger {background:#FFFFFF;color:#BD2B37;border:1px solid #EDCCD0;}
QPushButton#danger:hover {background:#FFF1F2;}
QPushButton#complete {background:#E8F6EE;color:#22774F;border:1px solid #B8DFC9;}
QPushButton#missing {background:#FDEBED;color:#AE3641;border:1px solid #F1C5CA;}
QPushButton#nav_active {background:#344352;border:1px solid #45586B;border-left:3px solid #E8BC3D;color:#FFFFFF;text-align:left;font-weight:700;border-radius:10px;padding:12px 14px;}
QPushButton#nav_idle {background:transparent;border:1px solid transparent;color:#C4CDD6;text-align:left;font-weight:550;border-radius:10px;padding:12px 14px;}
QPushButton#nav_idle:hover {background:#2A3847;color:#FFFFFF;}
QPushButton#nav_footer {background:#2D3A49;border:1px solid #3E4B5B;color:#DEE5ED;text-align:left;}
QFrame#sidebar {background:#1D2936;border:1px solid #253443;border-radius:17px;}
QFrame#sidebar QLabel {background:transparent;color:#DDE5ED;}
QFrame#sidebar QLabel#sidebarBrand {font-size:18px;font-weight:800;color:#FFFFFF;}
QFrame#sidebar QLabel#sidebarNote {font-size:11px;color:#97A6B5;}
QLabel#brandMark {background:transparent;border:none;}
QLineEdit,QSpinBox,QComboBox {background:#FFFFFF;color:#243344;border:1px solid #DCE3E9;padding:6px 8px;border-radius:8px;selection-background-color:#F3DCE0;}
QLineEdit:focus,QSpinBox:focus,QComboBox:focus {border:1px solid #C84B53;}
QTableView {background:#FFFFFF;alternate-background-color:#FBFCFD;gridline-color:#EDF0F2;border:1px solid #E0E5EA;border-radius:12px;selection-background-color:#FCE8EA;selection-color:#253340;}
QHeaderView::section {background:#F4F6F8;padding:7px;border:none;border-bottom:1px solid #E3E8ED;font-weight:700;color:#526172;}
QScrollBar:vertical {width:15px;background:#F1F3F5;}
QScrollBar::handle:vertical {background:#BBC5CF;min-height:38px;border-radius:6px;margin:3px;}
QScrollBar:horizontal {height:15px;background:#F1F3F5;}
QScrollBar::handle:horizontal {background:#BBC5CF;min-width:38px;border-radius:6px;margin:3px;}
QScrollArea {border:none;background:transparent;}
QFrame#inventoryCard {background:#FFFFFF;border:1px solid #DEE4E9;border-radius:11px;}
QFrame#inventoryCard[isComplete="true"] {border-color:#B4D9C3;}
QLabel#sectionHeading {font-size:15px;font-weight:700;color:#344251;}
QLabel#fieldHeading {color:#445567;font-weight:650;font-size:13px;}
QLabel#partNumber {color:#52667C;font-size:12px;font-weight:650;}
QDialog {background:#F5F6F8;}
QTabWidget::pane {border:0;}
QTabBar::tab {padding:11px 17px;background:#EDF0F3;color:#526172;border-radius:9px;margin-right:5px;}
QTabBar::tab:selected {background:#D92331;color:#FFFFFF;font-weight:700;}
QStatusBar {background:#F5F6F8;color:#718092;border-top:1px solid #E4E8ED;}
"""


# Neutral SVG line icons: consistent stroke width and color across navigation.
_NAV_PATHS = {
    'search':'<circle cx="10.8" cy="10.8" r="6.7"/><path d="m16 16 5 5"/>',
    'collection':'<rect x="3" y="4" width="8" height="8" rx="1.5"/><rect x="13" y="4" width="8" height="8" rx="1.5"/><rect x="3" y="14" width="8" height="7" rx="1.5"/><rect x="13" y="14" width="8" height="7" rx="1.5"/>',
    'missing':'<path d="M12 3 2.5 20.5h19L12 3Z"/><path d="M12 9v5"/><circle cx="12" cy="17.3" r="0.7" fill="currentColor" stroke="none"/>',
    'loose':'<path d="M3 8 12 3l9 5v10l-9 5-9-5V8Z"/><path d="m3 8 9 5 9-5M12 13v10"/>',
    'stats':'<path d="M4 20V12h4v8M10 20V4h4v16M16 20v-9h4v9M2 20h20"/>',
    'settings':'<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9 7 7M17 17l2.1 2.1M19.1 4.9 17 7M7 17l-2.1 2.1"/>',
    'reverse':'<path d="M4 10V5h5M20 14v5h-5"/><path d="M5 8a8 8 0 0 1 13-2M19 16A8 8 0 0 1 6 18"/>',
    'finish':'<path d="M4 13 9 18 20 6"/>',
    'undo':'<path d="M9 5 3 11l6 6M3 11h11a7 7 0 0 1 0 14"/>',
}
def navigation_icon(name, active=False):
    path=_NAV_PATHS.get(name,_NAV_PATHS['collection'])
    color='#FFFFFF' if active else '#B5C1CC'
    svg=f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><g fill="none" stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{path}</g></svg>'
    renderer=QSvgRenderer(QByteArray(svg.encode('utf-8')))
    canvas=QPixmap(24,24);canvas.fill(Qt.transparent)
    painter=QPainter(canvas)
    renderer.render(painter)
    painter.end()
    return QIcon(canvas)


def make_button(text, handler=None, primary=False):
    b = QPushButton(text)
    b.setMinimumHeight(34)
    if primary: b.setObjectName('primary')
    if handler: b.clicked.connect(handler)
    return b

def label(text='', heading=False, muted=False):
    x = QLabel(text); x.setWordWrap(True)
    if heading: x.setObjectName('heading')
    elif muted: x.setObjectName('muted')
    return x

def panel_layout():
    f = QFrame(); f.setObjectName('panel')
    v = QVBoxLayout(f); v.setContentsMargins(16,16,16,16); v.setSpacing(10)
    return f, v

class Store:
    def __init__(self):
        self.db = sqlite3.connect(DB_PATH)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.create()
        self.undo = None
        self.backup()
    def create(self):
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS sets(id INTEGER PRIMARY KEY AUTOINCREMENT, set_num TEXT NOT NULL,
          name TEXT NOT NULL, year INTEGER DEFAULT 0, image TEXT DEFAULT '', started INTEGER DEFAULT 0, added TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS parts(id INTEGER PRIMARY KEY AUTOINCREMENT, set_id INTEGER NOT NULL REFERENCES sets(id) ON DELETE CASCADE,
          part_num TEXT NOT NULL, color_id INTEGER NOT NULL, name TEXT NOT NULL, color TEXT NOT NULL DEFAULT '', image TEXT DEFAULT '',
          category TEXT DEFAULT '', required INTEGER NOT NULL DEFAULT 0, owned INTEGER NOT NULL DEFAULT 0,
          spare INTEGER NOT NULL DEFAULT 0, alternate INTEGER NOT NULL DEFAULT 0, UNIQUE(set_id,part_num,color_id,spare,alternate));
        CREATE TABLE IF NOT EXISTS loose(part_num TEXT NOT NULL, color_id INTEGER NOT NULL, name TEXT NOT NULL, color TEXT NOT NULL DEFAULT '',
          image TEXT DEFAULT '', quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(part_num,color_id));
        CREATE INDEX IF NOT EXISTS idx_parts_key ON parts(part_num,color_id);
        CREATE INDEX IF NOT EXISTS idx_parts_set ON parts(set_id);
        ''')
        self.db.commit()
    def backup(self):
        if not DB_PATH.exists() or not self.db.execute('SELECT 1 FROM sets LIMIT 1').fetchone(): return
        dest = BACKUPS / f'collection_{datetime.now():%Y%m%d_%H%M%S}.sqlite3'
        target=sqlite3.connect(dest)
        try: self.db.backup(target)
        finally: target.close()
        for p in sorted(BACKUPS.glob('collection_*.sqlite3'),reverse=True)[12:]: p.unlink(missing_ok=True)
    def rows(self, sql, params=()): return [dict(x) for x in self.db.execute(sql,params).fetchall()]
    def row(self, sql, params=()):
        x=self.db.execute(sql,params).fetchone(); return dict(x) if x else None
    def count(self,sql,params=()): return self.db.execute(sql,params).fetchone()[0]
    def add_set(self, entry, parts):
        with self.db:
            cur=self.db.execute('INSERT INTO sets(set_num,name,year,image) VALUES(?,?,?,?)',
                (entry['set_num'],entry['name'],entry.get('year') or 0,entry.get('set_img_url') or ''))
            sid=cur.lastrowid
            agg={}
            for raw in parts:
                if not isinstance(raw,dict): continue
                p=raw.get('part') or {}
                if not isinstance(p,dict): continue
                part_num=str(p.get('part_num') or '').strip()
                color=raw.get('color') or {}
                cid=int(color.get('id') or 0) if isinstance(color,dict) else 0
                if not part_num: continue
                spare=int(bool(raw.get('is_spare')))
                alt=int(bool(raw.get('is_alternate')))
                key=(part_num,cid,spare,alt)
                qty=max(0,int(raw.get('quantity') or 0))
                if key not in agg:
                    agg[key]=[str(p.get('name') or part_num),str(color.get('name') or '') if isinstance(color,dict) else '',
                        str(raw.get('part_img_url') or p.get('part_img_url') or ''), str((p.get('part_cat_id') or '')),0]
                agg[key][4]+=qty
            for (num,cid,spare,alt),(name,col,img,cat,qty) in agg.items():
                self.db.execute('''INSERT INTO parts(set_id,part_num,color_id,name,color,image,category,required,spare,alternate)
                                 VALUES(?,?,?,?,?,?,?,?,?,?)''',(sid,num,cid,name,col,img,cat,qty,spare,alt))
        return sid
    def image_urls_for_set(self, sid):
        urls = {r['image'] for r in self.rows('SELECT image FROM parts WHERE set_id=?', (sid,)) if r['image']}
        entry = self.row('SELECT image FROM sets WHERE id=?', (sid,))
        if entry and entry['image']:
            urls.add(entry['image'])
        return urls

    def image_is_used(self, url):
        return bool(self.row('SELECT 1 AS found FROM sets WHERE image=? LIMIT 1', (url,))
                    or self.row('SELECT 1 AS found FROM parts WHERE image=? LIMIT 1', (url,))
                    or self.row('SELECT 1 AS found FROM loose WHERE image=? AND quantity>0 LIMIT 1', (url,)))

    def delete_set(self, sid):
        # The primary key identifies one physical copy, even if two copies have
        # the same LEGO set number. The FK cascade removes only its parts.
        target=self.row('SELECT id FROM sets WHERE id=?',(sid,))
        if not target:
            raise ValueError('Комплектът вече не съществува.')
        # A unique, restorable, consistent backup MUST succeed before deletion.
        stamp=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        backup_file=BACKUPS / f'pre_delete_set_{sid}_{stamp}.sqlite3'
        backup_db=sqlite3.connect(str(backup_file))
        try:
            self.db.backup(backup_db)
        finally:
            backup_db.close()
        with self.db:
            self.db.execute('DELETE FROM sets WHERE id=?',(sid,))
        # The ordinary undo state can refer to deleted part IDs; discard it.
        self.undo=None
        return backup_file

    def start(self,sid):
        old=self.row('SELECT started FROM sets WHERE id=?',(sid,))
        self.undo=('start',sid,old['started'])
        with self.db: self.db.execute('UPDATE sets SET started=1 WHERE id=?',(sid,))
    def set_owned(self,pid,num):
        x=self.row('SELECT owned,required FROM parts WHERE id=?',(pid,))
        if not x: return
        n=max(0,min(int(num),x['required']))
        if n==x['owned']: return
        self.undo=('part',pid,x['owned'])
        with self.db: self.db.execute('UPDATE parts SET owned=? WHERE id=?',(n,pid))
    def set_loose(self,entry,qty):
        num,cid=entry['part_num'],entry['color_id']
        old=self.row('SELECT quantity FROM loose WHERE part_num=? AND color_id=?',(num,cid))
        self.undo=('loose',dict(entry),old['quantity'] if old else 0)
        with self.db:
            self.db.execute('''INSERT INTO loose(part_num,color_id,name,color,image,quantity) VALUES(?,?,?,?,?,?)
                ON CONFLICT(part_num,color_id) DO UPDATE SET quantity=excluded.quantity''',
                (num,cid,entry['name'],entry['color'],entry['image'],max(0,int(qty))))
    def add_loose_quantity(self,entry,quantity):
        # Increment an existing row without overwriting stock.
        qty=int(quantity)
        if qty<=0 or qty>100000: raise ValueError('Въведи количество между 1 и 100000.')
        num=str(entry['part_num']).strip()
        cid=int(entry['color_id'])
        if not num or cid<0: raise ValueError('Въведи валиден номер и цвят.')
        old=self.row('SELECT * FROM loose WHERE part_num=? AND color_id=?',(num,cid))
        if old and int(old['quantity'])+qty>1000000:
            raise ValueError('Максималното общо количество е 1 000 000.')
        self.undo=('loose',dict(entry),old['quantity'] if old else 0)
        with self.db:
            self.db.execute('''INSERT INTO loose(part_num,color_id,name,color,image,quantity)
                VALUES(?,?,?,?,?,?) ON CONFLICT(part_num,color_id) DO UPDATE SET
                quantity=loose.quantity+excluded.quantity,
                name=CASE WHEN loose.name='' OR loose.name=loose.part_num THEN excluded.name ELSE loose.name END,
                color=CASE WHEN loose.color='' THEN excluded.color ELSE loose.color END,
                image=CASE WHEN loose.image='' THEN excluded.image ELSE loose.image END''',
                (num,cid,str(entry.get('name') or num)[:255],str(entry.get('color') or f'Цвят #{cid}')[:120],
                 str(entry.get('image') or ''),qty))
        return int(old['quantity'])+qty if old else qty

    def delete_loose(self,part_num,color_id):
        row=self.row('SELECT * FROM loose WHERE part_num=? AND color_id=?',(part_num,color_id))
        if not row: return None
        self.undo=('loose_deleted',dict(row))
        with self.db:
            self.db.execute('DELETE FROM loose WHERE part_num=? AND color_id=?',(part_num,color_id))
        return row

    def search_known_parts(self,number='',description='',shape='',color='',limit=60):
        # Bounded local search.
        filters=[];args=[]
        if number.strip():
            filters.append('LOWER(p.part_num) LIKE ?');args.append('%'+number.strip().lower()+'%')
        if description.strip():
            for token in description.lower().split():
                filters.append('LOWER(p.name) LIKE ?');args.append('%'+token+'%')
        if shape.strip():
            normalized=shape.casefold().replace('×','x').replace(' ','').replace('-','')
            expr="REPLACE(REPLACE(REPLACE(LOWER(p.name),' ',''),'×','x'),'-','')"
            filters.append(expr+' LIKE ?');args.append('%'+normalized+'%')
        if color.strip():
            filters.append('LOWER(p.color) LIKE ?');args.append('%'+color.strip().lower()+'%')
        if not filters: return []
        sql='''SELECT part_num,color_id,name,color,MAX(image) AS image FROM (
              SELECT part_num,color_id,name,color,image FROM parts
              UNION ALL SELECT part_num,color_id,name,color,image FROM loose
              ) p WHERE '''+' AND '.join(filters)+''' GROUP BY part_num,color_id
              ORDER BY CASE WHEN MAX(image)<>'' THEN 0 ELSE 1 END,name,color LIMIT ?'''
        args.append(min(80,max(1,int(limit))))
        return self.rows(sql,args)

    def move(self,part_num,cid,source_id,target_id,qty):
        """source/target are part row ids, 'loose', or None for newly found; all atomic."""
        qty=int(qty)
        if qty<=0: raise ValueError('Въведи количество над нула.')
        changes=[]
        with self.db:
            for entity,delta in ((source_id,-qty),(target_id,qty)):
                if entity is None: continue
                if entity=='loose':
                    old=self.row('SELECT * FROM loose WHERE part_num=? AND color_id=?',(part_num,cid))
                    if not old: raise ValueError('Няма налични свободни части от този вид.')
                    n=old['quantity']+delta
                    if n<0: raise ValueError('Няма достатъчно свободни части.')
                    changes.append(('loose',part_num,cid,old['quantity']))
                    self.db.execute('UPDATE loose SET quantity=? WHERE part_num=? AND color_id=?',(n,part_num,cid))
                else:
                    r=self.row('SELECT * FROM parts WHERE id=?',(entity,))
                    if not r or (r['part_num'],r['color_id'])!=(part_num,cid): raise ValueError('Несъвместима част или цвят.')
                    n=r['owned']+delta
                    if n<0 or n>r['required']: raise ValueError('Невъзможно количество в комплекта.')
                    changes.append(('part',entity,r['owned']))
                    self.db.execute('UPDATE parts SET owned=? WHERE id=?',(n,entity))
        self.undo=('move',changes)
    def undo_last(self):
        action=self.undo
        if not action: return False
        with self.db:
            if action[0]=='part': self.db.execute('UPDATE parts SET owned=? WHERE id=?',(action[2],action[1]))
            elif action[0]=='start': self.db.execute('UPDATE sets SET started=? WHERE id=?',(action[2],action[1]))
            elif action[0]=='loose':
                e=action[1]; self.db.execute('UPDATE loose SET quantity=? WHERE part_num=? AND color_id=?',(action[2],e['part_num'],e['color_id']))
            elif action[0]=='loose_deleted':
                x=action[1]
                self.db.execute('''INSERT INTO loose(part_num,color_id,name,color,image,quantity)
                  VALUES(?,?,?,?,?,?) ON CONFLICT(part_num,color_id) DO UPDATE SET
                  name=excluded.name,color=excluded.color,image=excluded.image,quantity=excluded.quantity''',
                  (x['part_num'],x['color_id'],x['name'],x['color'],x['image'],x['quantity']))
            elif action[0]=='move':
                for x in action[1]:
                    if x[0]=='part': self.db.execute('UPDATE parts SET owned=? WHERE id=?',(x[2],x[1]))
                    else: self.db.execute('UPDATE loose SET quantity=? WHERE part_num=? AND color_id=?',(x[3],x[1],x[2]))
        self.undo=None; return True
    def all_sets(self):
        return self.rows('''SELECT s.*,COALESCE(SUM(CASE WHEN p.spare=0 AND p.alternate=0 THEN p.required ELSE 0 END),0) AS total,
          COALESCE(SUM(CASE WHEN p.spare=0 AND p.alternate=0 THEN p.owned ELSE 0 END),0) AS owned
          FROM sets s LEFT JOIN parts p ON p.set_id=s.id GROUP BY s.id ORDER BY s.added DESC,s.id DESC''')
    def parts_for(self,sid): return self.rows('SELECT * FROM parts WHERE set_id=? ORDER BY spare,alternate,color,part_num',(sid,))
    def loose_rows(self): return self.rows('SELECT * FROM loose WHERE quantity>0 ORDER BY name,color')
    def missing(self,spares=False):
        return self.rows('''SELECT p.part_num,p.color_id,p.name,p.color,MAX(p.image) image,
         SUM(p.required-p.owned) missing,COUNT(DISTINCT p.set_id) num_sets
         FROM parts p JOIN sets s ON s.id=p.set_id
         WHERE s.started=1 AND p.spare=? AND p.alternate=0 AND p.required>p.owned
         GROUP BY p.part_num,p.color_id ORDER BY missing DESC,p.name''',(int(spares),))
    def where_used(self,num,cid,only_missing=False,spare=None):
        sql='''SELECT p.*,s.name set_name,s.set_num,s.started FROM parts p JOIN sets s ON s.id=p.set_id
          WHERE p.part_num=? AND p.color_id=? AND p.alternate=0'''
        args=[num,cid]
        if only_missing: sql+=' AND s.started=1 AND p.required>p.owned'
        if spare is not None: sql+=' AND p.spare=?';args.append(int(spare))
        return self.rows(sql+' ORDER BY s.name',args)

class TaskSignals(QObject):
    success=Signal(object)
    error=Signal(str)

class Task(QRunnable):
    def __init__(self,func): super().__init__();self.func=func;self.signals=TaskSignals()
    def run(self):
        try: self.signals.success.emit(self.func())
        except Exception as e: self.signals.error.emit(str(e))

class OfflineSignals(QObject):
    progress = Signal(int, int, int)
    done = Signal(int, int, int)
    error = Signal(str)

class OfflineImagesTask(QRunnable):
    """Downloads cached collection images off the UI thread, sequentially and safely."""
    def __init__(self, urls):
        super().__init__()
        self.urls=list(urls)
        self.signals=OfflineSignals()
    def run(self):
        import requests
        total=len(self.urls)
        downloaded=0
        failed=0
        session=requests.Session()
        try:
            for i,url in enumerate(self.urls,1):
                path=image_cache_path(url)
                try:
                    if not url.startswith('https://'):
                        raise ValueError('Invalid image URL')
                    if not path.is_file() or path.stat().st_size==0:
                        response=session.get(url,timeout=(8,25))
                        response.raise_for_status()
                        cache_image_bytes(url, response.content)
                        downloaded+=1
                except Exception:
                    failed+=1
                    try: path.with_suffix(path.suffix+'.offline_tmp').unlink(missing_ok=True)
                    except Exception: pass
                self.signals.progress.emit(i,total,failed)
            self.signals.done.emit(downloaded,failed,total)
        except Exception as ex:
            self.signals.error.emit(str(ex))
        finally:
            session.close()

class Api:
    def __init__(self): self.key=self.settings().get('api_key','')
    @staticmethod
    def settings():
        try: return json.loads(SETTINGS_PATH.read_text(encoding='utf8'))
        except (FileNotFoundError,ValueError): return {}
    def save_key(self,k):
        self.key=k.strip()
        settings=self.settings()
        settings['api_key']=self.key
        SETTINGS_PATH.write_text(json.dumps(settings,ensure_ascii=False,indent=2),encoding='utf8')
    @staticmethod
    def check_key(candidate):
        """Validate without persisting the candidate key."""
        candidate = candidate.strip()
        if not candidate:
            raise ValueError('Постави API ключ, преди да го провериш.')
        if len(candidate) > 256 or any(c.isspace() for c in candidate):
            raise ValueError('Ключът съдържа невалидни символи.')
        try:
            response = requests.get(
                API_BASE + 'sets/',
                params={'page_size': 1},
                headers={'Authorization': 'key ' + candidate,
                         'User-Agent': 'LEGOCollectionManager/1.7'},
                timeout=15,
            )
        except requests.RequestException:
            raise ValueError('Няма връзка с Rebrickable. Провери интернет и опитай отново.') from None
        if response.status_code in (401, 403):
            raise ValueError('Ключът не е приет от Rebrickable. Провери дали е копиран правилно.')
        if response.status_code == 429:
            raise ValueError('Rebrickable временно ограничава заявките. Опитай по-късно.')
        if not response.ok:
            raise ValueError(f'Неуспешна проверка на Rebrickable (HTTP {response.status_code}).')
        return candidate

    def get(self,path,params=None):
        if not self.key: raise ValueError('Първо въведи API ключ в Настройки.')
        url=API_BASE+path.lstrip('/')
        response=requests.get(url,params=params or {},headers={'Authorization':f'key {self.key}','User-Agent':'LEGOCollectionManager/1.0'},timeout=25)
        if response.status_code==401: raise ValueError('Невалиден API ключ (401). Провери Настройки.')
        if response.status_code==429: raise ValueError('API лимит: опитай отново след малко (429).')
        response.raise_for_status()
        return response.json()
    def paged(self,path,params=None):
        if not self.key: raise ValueError('Първо въведи API ключ в Настройки.')
        collected=[]; url=API_BASE+path.lstrip('/'); opts=dict(params or {}); opts['page_size']=1000
        for i in range(60):
            r=requests.get(url,params=opts if i==0 else None,headers={'Authorization':f'key {self.key}', 'User-Agent':'LEGOCollectionManager/1.0'},timeout=35)
            r.raise_for_status(); data=r.json()
            collected.extend(data.get('results') or [])
            url=data.get('next');opts=None
            if not url: break
            if urlparse(url).hostname != 'rebrickable.com': raise ValueError('Неочакван адрес за следваща страница.')
        else: raise RuntimeError('Прекалено много страници от API.')
        return collected
    def search(self,q): return self.paged('sets/',{'search':q,'page_size':100})[:100]
    def inventory(self,set_num):
        # inc_minifig_parts expands minifig components into parts for checking individually.
        return self.paged(f'sets/{set_num}/parts/',{'inc_minifig_parts':1})

class Images(QObject):
    updated=Signal(str)
    def __init__(self,pool):
        super().__init__()
        self.pool=pool
        self.loading=set();self.active_tasks=set();self.pending=[]
        self.inflight=0;self.max_parallel=3;self.stopping=False
        self.pixmaps=OrderedDict()
        self.cache_bytes=0
        self.failed_until={}
        self.cleanup_db=None
        self.cleanup_pending=set()
    def _cache_put(self,key,pix):
        weight=pix.width()*pix.height()*4
        if key in self.pixmaps:
            old=self.pixmaps.pop(key)
            self.cache_bytes-=old.width()*old.height()*4
        self.pixmaps[key]=pix
        self.cache_bytes+=weight
        while self.pixmaps and (self.cache_bytes>MAX_MEMORY_CACHE_BYTES or len(self.pixmaps)>150):
            _,old=self.pixmaps.popitem(last=False)
            self.cache_bytes-=old.width()*old.height()*4
    def get(self,url,size=(110,110)):
        if not url:return QPixmap()
        size=(max(1,int(size[0])),max(1,int(size[1])))
        key=(url,size)
        if key in self.pixmaps:
            self.pixmaps.move_to_end(key)
            return self.pixmaps[key]
        path=image_cache_path(url)
        if path.is_file():
            # Decode at the desired display size; never decode an oversized source
            # into a huge QPixmap simply to scale it down afterwards.
            from PySide6.QtGui import QImageReader
            reader=QImageReader(str(path))
            original=reader.size()
            if original.isValid() and (original.width()>size[0] or original.height()>size[1]):
                bounded=original.scaled(QSize(*size),Qt.KeepAspectRatio)
                reader.setScaledSize(bounded)
            image=reader.read()
            if not image.isNull():
                pix=QPixmap.fromImage(image)
                if pix.width()>size[0] or pix.height()>size[1]:
                    pix=pix.scaled(*size,Qt.KeepAspectRatio,Qt.SmoothTransformation)
                self._cache_put(key,pix)
                return pix
        if (not self.stopping and url not in self.loading and url.startswith('https://')
                and self.failed_until.get(url,0)<time.monotonic()):
            self.loading.add(url)
            self.pending.append(url)
            self.launch_pending()
        return QPixmap()
    def launch_pending(self):
        while not self.stopping and self.inflight<self.max_parallel and self.pending:
            url=self.pending.pop(0)
            self.inflight+=1
            def fetch(u=url):
                res=requests.get(u,timeout=(7,16))
                res.raise_for_status()
                cache_image_bytes(u,res.content)
                return u
            task=Task(fetch)
            self.active_tasks.add(task)
            task.signals.success.connect(lambda value,t=task:self.finish_task(t,value))
            task.signals.error.connect(lambda err,t=task,u=url:self.finish_task(t,u,False))
            self.pool.start(task)
    def cleanup_unused(self, db, urls):
        # Never delete an image still referenced by another copy of a set,
        # another set, or the free-parts inventory.
        self.cleanup_db=db
        removed=0
        for url in urls:
            if not url or db.image_is_used(url):
                self.cleanup_pending.discard(url)
                continue
            if url in self.loading:
                self.cleanup_pending.add(url)
                # Prevent queued downloads from starting after deletion.
                if url in self.pending:
                    self.pending=[x for x in self.pending if x!=url]
                    self.loading.discard(url)
                    self.cleanup_pending.discard(url)
                else:
                    continue
            self.cleanup_pending.discard(url)
            for key in [key for key in self.pixmaps if key[0]==url]:
                pix=self.pixmaps.pop(key)
                self.cache_bytes-=pix.width()*pix.height()*4
            try:
                cache_file=image_cache_path(url)
                if cache_file.exists():
                    cache_file.unlink()
                    removed+=1
            except OSError:
                pass
        return removed

    def finish_task(self,task,url,ok=True):
        self.loading.discard(url)
        self.active_tasks.discard(task)
        self.inflight=max(0,self.inflight-1)
        if not ok:self.failed_until[url]=time.monotonic()+90
        if url in self.cleanup_pending and self.cleanup_db is not None:
            self.cleanup_unused(self.cleanup_db, {url})
        if ok and not self.stopping and url not in self.cleanup_pending:
            self.updated.emit(url)
        self.launch_pending()


def picture(images,url,w=110,h=90,lazy=False):
    pic=QLabel();pic.setFixedSize(w,h);pic.setAlignment(Qt.AlignCenter)
    pic.setProperty('image_url',url)
    if lazy:
        pic.setProperty('lazy_image_pending',True)
        pic.setText('…')
    else:
        pm=images.get(url,(w-8,h-8))
        if not pm.isNull(): pic.setPixmap(pm)
        else: pic.setText('·')
    return pic

class ImageButtonDelegate(QStyledItemDelegate):
    def __init__(self, owner, image_column=0, buttons=()):
        super().__init__(owner);self.owner=owner;self.image_column=image_column;self.buttons=buttons
    def paint(self,painter,option,index):
        c=index.column()
        if c==self.image_column:
            painter.save()
            if option.state & QStyle.State_Selected:
                painter.fillRect(option.rect,QColor('#e8edf6'))
            else:
                painter.fillRect(option.rect,QColor('#fafbfc' if index.row()%2 else '#ffffff'))
            pm=index.data(Qt.DecorationRole)
            if isinstance(pm,QIcon):pm=pm.pixmap(option.rect.size())
            if isinstance(pm,QPixmap) and not pm.isNull():
                margin=6
                pix=pm.scaled(option.rect.width()-2*margin,option.rect.height()-2*margin,Qt.KeepAspectRatio,Qt.SmoothTransformation)
                x=option.rect.x()+(option.rect.width()-pix.width())//2
                y=option.rect.y()+(option.rect.height()-pix.height())//2
                painter.drawPixmap(x,y,pix)
            else:
                painter.setPen(QColor('#a1abb8'))
                painter.drawText(option.rect,Qt.AlignCenter,'…')
            painter.restore()
            return
        # Compact status pills: status colors are limited to their own cells.
        # They never paint whole rows, keeping the interface calm and readable.
        status_cell = (isinstance(index.model(),PartModel) and c in (5,9)) or (isinstance(index.model(),MissingModel) and c==3)
        if status_cell:
            painter.save()
            if option.state & QStyle.State_Selected:
                painter.fillRect(option.rect,QColor('#e8f0fb'))
            else:
                painter.fillRect(option.rect,QColor('#ffffff' if index.row()%2==0 else '#fafcff'))
            if isinstance(index.model(),PartModel):
                part=index.model().items[index.row()]
                required=int(part['required']);owned=int(part['owned'])
                complete=owned>=required
                if c==9 and not complete:
                    fill,border,ink='#f2f5f8','#d8e0e8','#8291a3'
                    text='✓'
                elif complete:
                    fill,border,ink='#d9f5e3','#79c99a','#17683b'
                    text='✓' if c==9 else '0'
                else:
                    fill,border,ink='#fde1e1','#eea3a8','#ae2937'
                    text=str(max(0,required-owned))
            else:
                fill,border,ink='#fde1e1','#eea3a8','#ae2937'
                text=str(index.data(Qt.DisplayRole) or '0')
            width=min(option.rect.width()-14,58 if c==9 else 98)
            height=34
            r=option.rect.adjusted((option.rect.width()-width)//2,(option.rect.height()-height)//2,
                                   -(option.rect.width()-width+1)//2,-(option.rect.height()-height+1)//2)
            painter.setPen(QPen(QColor(border)))
            painter.setBrush(QColor(fill))
            painter.drawRoundedRect(r,10,10)
            painter.setPen(QColor(ink))
            painter.drawText(r,Qt.AlignCenter,text)
            painter.restore()
            return
        if c in self.buttons:
            painter.save()
            if option.state & QStyle.State_Selected:painter.fillRect(option.rect,QColor('#e8f0fb'))
            r=option.rect.adjusted(7, max(8,(option.rect.height()-35)//2),-7,-max(8,(option.rect.height()-35)//2))
            painter.setPen(QPen(QColor('#cbd5e1')))
            painter.setBrush(QColor('#f7f9fc'))
            painter.drawRoundedRect(r,8,8)
            painter.setPen(QColor('#273242'))
            painter.drawText(r,Qt.AlignCenter,str(index.data(Qt.DisplayRole) or ''))
            painter.restore()
            return
        super().paint(painter,option,index)

class MissingSourcesDelegate(QStyledItemDelegate):
    """Paint compact, readable set tiles. No giant composite pixmap or detached counters."""
    TILE_W = 108
    TILE_H = 87
    IMAGE_W = 80
    IMAGE_H = 54

    def columns(self, width):
        return max(1, int(max(1, width - 14) // self.TILE_W))

    def row_height(self, count, width):
        if not count:
            return 72
        return max(94, 12 + ((count + self.columns(width) - 1) // self.columns(width)) * self.TILE_H)

    def paint(self, painter, option, index):
        if not index.isValid():
            return
        model = index.model()
        entries = model.sources_for(index.row())
        painter.save()
        rect = option.rect
        painter.fillRect(rect, QColor('#F0F4F7') if option.state & QStyle.State_Selected else QColor('#FFFFFF' if index.row() % 2 == 0 else '#FBFCFD'))
        painter.setClipRect(rect.adjusted(3, 2, -3, -2))
        if not entries:
            painter.setPen(QColor('#80909F'))
            painter.drawText(rect.adjusted(12, 0, -8, 0), Qt.AlignVCenter, 'Няма данни')
            painter.restore()
            return
        columns = self.columns(rect.width())
        for i, source in enumerate(entries):
            x = rect.x() + 8 + (i % columns) * self.TILE_W
            y = rect.y() + 5 + (i // columns) * self.TILE_H
            pic_rect = QRect(x + 12, y + 2, self.IMAGE_W, self.IMAGE_H)
            pixmap = model.images.get(source.get('image', ''), (self.IMAGE_W, self.IMAGE_H))
            if not pixmap.isNull():
                scaled = pixmap.scaled(self.IMAGE_W, self.IMAGE_H, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                px = pic_rect.x() + (pic_rect.width() - scaled.width()) // 2
                py = pic_rect.y() + (pic_rect.height() - scaled.height()) // 2
                painter.drawPixmap(px, py, scaled)
            else:
                painter.fillRect(pic_rect, QColor('#F3F5F7'))
            painter.setPen(QColor('#253342'))
            font = painter.font(); font.setPointSize(9); font.setBold(True)
            painter.setFont(font)
            painter.drawText(x, y + 58, self.TILE_W - 3, 14, Qt.AlignCenter, str(source['set_num']))
            font.setBold(False); font.setPointSize(8); painter.setFont(font)
            painter.setPen(QColor('#6C7885'))
            painter.drawText(x, y + 72, self.TILE_W - 3, 12, Qt.AlignCenter, f"Липсват {source['missing']}")
        painter.restore()

    def sizeHint(self, option, index):
        model=index.model()
        return QSize(option.rect.width(), self.row_height(len(model.sources_for(index.row())), option.rect.width()))


class PartModel(QAbstractTableModel):
    headers=['Снимка','Част / номер','Цвят','Общо','Налични','Липсват','Вид','−','+','✓']
    def __init__(self,owner): super().__init__();self.owner=owner;self.items=[]
    def set_items(self,items):
        self.beginResetModel();self.items=items;self.endResetModel()
    def rowCount(self,parent=QModelIndex()): return 0 if parent.isValid() else len(self.items)
    def columnCount(self,parent=QModelIndex()): return 10
    def headerData(self,section,orient,role=Qt.DisplayRole):
        return self.headers[section] if orient==Qt.Horizontal and role==Qt.DisplayRole else None
    def data(self,idx,role=Qt.DisplayRole):
        if not idx.isValid(): return None
        p=self.items[idx.row()];c=idx.column()
        if role==Qt.DecorationRole and c==0:
            pm=self.owner.images.get(p['image'],(185,135))
            return pm if not pm.isNull() else None
        if role in (Qt.DisplayRole,Qt.EditRole):
            vals=['',f"{p['name'][:48] + ('…' if len(p['name'])>48 else '')}\n#{p['part_num']}",p['color'],p['required'],p['owned'],max(0,p['required']-p['owned']),
                  'Резервна' if p['spare'] else ('Алтернатива' if p['alternate'] else 'Основна'),'−','+','✓']
            return vals[c]
        if role==Qt.TextAlignmentRole and c in [3,4,5,7,8,9]: return Qt.AlignCenter
        if role==Qt.ToolTipRole:return f"{p['name']}\n#{p['part_num']} · {p['color']}"
        return None
    def flags(self,idx):
        base=Qt.ItemIsEnabled|Qt.ItemIsSelectable
        if idx.column()==4 and self.owner.current_set and self.owner.current_set['started'] and not self.items[idx.row()]['alternate']:
            base|=Qt.ItemIsEditable
        return base
    def setData(self,idx,value,role=Qt.EditRole):
        if role!=Qt.EditRole or idx.column()!=4:return False
        try: v=int(value)
        except (TypeError,ValueError): return False
        p=self.items[idx.row()];self.owner.db.set_owned(p['id'],v);p['owned']=max(0,min(v,p['required']))
        self.dataChanged.emit(self.index(idx.row(),4),self.index(idx.row(),5));self.owner.on_data_changed(refresh_parts=False)
        return True

class QuantityDialog(QDialog):
    def __init__(self,parent,db,p,images,on_save):
        super().__init__(parent);self.p=p;self.db=db;self.on_save=on_save
        self.setWindowTitle('Проверка на част');self.resize(450,410)
        v=QVBoxLayout(self);v.setSpacing(14)
        h=QHBoxLayout();h.addWidget(picture(images,p['image'],125,110))
        h.addWidget(label(f"<b>{html.escape(p['name'])}</b><br>№ {html.escape(p['part_num'])}<br>{html.escape(p['color'])}<br>Необходими: {p['required']}"));v.addLayout(h)
        r=QHBoxLayout();self.spin=QSpinBox();self.spin.setRange(0,p['required']);self.spin.setValue(p['owned']);self.spin.setMinimumHeight(48)
        self.spin.setButtonSymbols(QSpinBox.NoButtons)
        r.addWidget(make_button('−',lambda:self.spin.setValue(self.spin.value()-1)))
        r.addWidget(self.spin,1);r.addWidget(make_button('+',lambda:self.spin.setValue(self.spin.value()+1)));v.addLayout(r)
        self.missing=label();v.addWidget(self.missing)
        self.spin.valueChanged.connect(lambda n:self.missing.setText(f'Липсват: {p["required"]-n} бр.'))
        self.spin.valueChanged.emit(self.spin.value())
        v.addWidget(make_button('✓ Всички налични',lambda:self.spin.setValue(p['required'])))
        v.addWidget(make_button('В кои други мои LEGO се използва?',self.where_used))
        v.addWidget(make_button('↔ Премести / вземи от свободни части',self.transfer))
        r=QHBoxLayout();r.addStretch();r.addWidget(make_button('Отказ',self.reject));r.addWidget(make_button('Запази',self.accept,True));v.addLayout(r)
    def where_used(self):
        lines=[f"{r['set_name']} ({r['set_num']}): {r['owned']}/{r['required']} налични"
               for r in self.db.where_used(self.p['part_num'],self.p['color_id'])]
        QMessageBox.information(self,'Тази част в моите LEGO','\n'.join(lines) or 'Няма съвпадения.')
    def transfer(self):
        self.reject()
        self.parent().open_transfer(self.p)
    def accept(self):
        self.db.set_owned(self.p['id'],self.spin.value());self.on_save();super().accept()

class TransferDialog(QDialog):
    def __init__(self,parent,db,num,cid,target=None):
        super().__init__(parent);self.db=db;self.num=num;self.cid=cid
        self.setWindowTitle('Преместване на части');self.resize(560,320)
        self.where=db.where_used(num,cid)
        self.loose=db.row('SELECT * FROM loose WHERE part_num=? AND color_id=?',(num,cid))
        v=QVBoxLayout(self);v.addWidget(label(f'Част {num} · Изберете откъде и накъде да преместите бройки.'))
        self.from_combo=QComboBox();self.to_combo=QComboBox()
        self.from_combo.addItem('Намерени нови части (извън инвентара)',None)
        self.from_combo.addItem(f'Свободни части ({self.loose["quantity"] if self.loose else 0})','loose')
        self.to_combo.addItem('Свободни части','loose')
        for p in self.where:
            name=f'{p["set_num"]} · {p["set_name"]} — {p["owned"]}/{p["required"]}'
            self.from_combo.addItem(name,p['id']);self.to_combo.addItem(name,p['id'])
        if target is not None:
            i=self.to_combo.findData(target)
            if i>=0:self.to_combo.setCurrentIndex(i)
        form=QFormLayout();form.addRow('От:',self.from_combo);form.addRow('Към:',self.to_combo)
        self.qty=QSpinBox();self.qty.setRange(1,100000);form.addRow('Количество:',self.qty);v.addLayout(form)
        v.addWidget(label('Преместването коригира и двата инвентара едновременно.',muted=True))
        b=QHBoxLayout();b.addStretch();b.addWidget(make_button('Отказ',self.reject));b.addWidget(make_button('Премести',self.apply,True));v.addLayout(b)
    def apply(self):
        source=self.from_combo.currentData();target=self.to_combo.currentData()
        if target is None:
            QMessageBox.warning(self,'Проверка','Избери получател.');return
        if source==target:
            QMessageBox.warning(self,'Проверка','Източникът и получателят трябва да са различни.');return
        if target=='loose' and not self.loose:
            # Establish loose record from known part metadata before transaction.
            p=self.where[0] if self.where else None
            if p:
                with self.db.db:self.db.db.execute('INSERT OR IGNORE INTO loose(part_num,color_id,name,color,image,quantity) VALUES(?,?,?,?,?,0)',
                           (self.num,self.cid,p['name'],p['color'],p['image']))
        try:
            self.db.move(self.num,self.cid,source,target,self.qty.value())
        except ValueError as e: QMessageBox.warning(self,'Не може да се премести',str(e));return
        self.accept()

class LoosePartSearchDialog(QDialog):
    # Search local catalogue or use an explicit online action.
    def __init__(self,owner):
        super().__init__(owner)
        self.owner=owner;self.selected=None;self.results=[];self.remote_serial=0
        self.setWindowTitle('Добавяне на свободни части · търси по снимка или описание')
        self.setMinimumSize(850,650);self.resize(1080,810)
        main=QVBoxLayout(self);main.setContentsMargins(18,18,18,18);main.setSpacing(10)
        main.addWidget(label('Намери частта по номер, описание, форма или цвят',heading=True))
        main.addWidget(label('Офлайн се търси сред частите от твоите комплекти и свободния запас. За нови части използвай онлайн търсене или въведи ръчно.',muted=True))
        filters=QGridLayout();filters.setSpacing(8)
        self.number=QLineEdit();self.number.setPlaceholderText('напр. 3001')
        self.description=QLineEdit();self.description.setPlaceholderText('напр. brick, plate, slope')
        self.shape=QLineEdit();self.shape.setPlaceholderText('напр. 2x4, 1x2, round')
        self.color_filter=QLineEdit();self.color_filter.setPlaceholderText('напр. Red, Blue, Black')
        for i,(title,w) in enumerate([('Номер',self.number),('Описание / вид',self.description),('Форма / размер',self.shape),('Цвят',self.color_filter)]):
            filters.addWidget(label(title),0,i);filters.addWidget(w,1,i)
        main.addLayout(filters)
        tools=QHBoxLayout();self.search_local=make_button('Търси в моя каталог',self.perform_local,True)
        self.search_online=make_button('🌐 Търси в Rebrickable',self.perform_online)
        tools.addWidget(self.search_local);tools.addWidget(self.search_online);tools.addStretch()
        self.info=label('Въведи поне едно условие за търсене.',muted=True)
        tools.addWidget(self.info);main.addLayout(tools)
        self.scroller=QScrollArea();self.scroller.setWidgetResizable(True)
        self.result_host=QWidget();self.result_grid=QGridLayout(self.result_host);self.result_grid.setSpacing(10);self.result_grid.setAlignment(Qt.AlignTop)
        self.scroller.setWidget(self.result_host);main.addWidget(self.scroller,1)
        # Results may start with placeholders; refresh their pictures asynchronously.
        self.owner.images.updated.connect(self.refresh_result_image)
        self.finished.connect(self.disconnect_pictures)
        self.pick_label=label('Избрана част: въведи номер или избери от снимките.',muted=True);main.addWidget(self.pick_label)
        details=QGridLayout();details.setSpacing(8)
        self.part_num=QLineEdit();self.part_num.setPlaceholderText('Задължително · напр. 3001')
        self.part_name=QLineEdit();self.part_name.setPlaceholderText('Описание, напр. Brick 2 x 4')
        self.presets=QComboBox();self.presets.addItem('Избери цвят',None)
        for title,cid in [('Black / Черен',0),('White / Бял',15),('Red / Червен',4),('Blue / Син',1),('Yellow / Жълт',14),('Green / Зелен',2),('Light Bluish Gray / Светлосив',71),('Dark Bluish Gray / Тъмносив',72),('Tan / Бежов',19),('Orange / Оранжев',25),('Reddish Brown / Кафяв',70)]:
            self.presets.addItem(title,(cid,title.split(' / ')[0]))
        self.presets.currentIndexChanged.connect(self.preset_change)
        self.color_id=QSpinBox();self.color_id.setRange(0,999999);self.color_id.setValue(0)
        self.color_name=QLineEdit();self.color_name.setPlaceholderText('напр. Red')
        self.qty=QSpinBox();self.qty.setRange(1,100000);self.qty.setValue(1);self.qty.setMinimumHeight(38)
        widgets=[('Номер на част *',self.part_num),('Име / вид',self.part_name),('Цвят (избери)',self.presets),('Цвят (ID) *',self.color_id),('Цвят (име)',self.color_name),('Добави бройки',self.qty)]
        for i,(title,widget) in enumerate(widgets):
            col=i%3;row=(i//3)*2
            details.addWidget(label(title),row,col);details.addWidget(widget,row+1,col)
        main.addLayout(details)
        self.stock_info=label('При вече съществуваща част новите бройки се прибавят към наличните.',muted=True)
        main.addWidget(self.stock_info)
        actions=QHBoxLayout();actions.addStretch()
        actions.addWidget(make_button('Отказ',self.reject))
        actions.addWidget(make_button('＋ Добави към свободните части',self.save,True))
        main.addLayout(actions)
        self.part_num.textChanged.connect(self.update_stock_info)
        self.color_id.valueChanged.connect(self.update_stock_info)
        self.typing_timer=QTimer(self);self.typing_timer.setSingleShot(True);self.typing_timer.setInterval(350)
        self.typing_timer.timeout.connect(self.perform_local)
        for w in (self.number,self.description,self.shape,self.color_filter):
            w.textChanged.connect(lambda *_:self.typing_timer.start())

    def preset_change(self,index):
        v=self.presets.itemData(index)
        if v is not None:
            self.color_id.setValue(v[0]);self.color_name.setText(v[1])

    def update_stock_info(self,*_):
        n=self.part_num.text().strip()
        if not n:return
        r=self.owner.db.row('SELECT quantity FROM loose WHERE part_num=? AND color_id=?',(n,self.color_id.value()))
        self.stock_info.setText((f'В свободния запас вече има {r["quantity"]} бр. – новото количество ще бъде прибавено.'
                                 if r else 'Нова част за свободния запас.'))

    def perform_local(self):
        fields=(self.number.text(),self.description.text(),self.shape.text(),self.color_filter.text())
        if not any(x.strip() for x in fields):
            self.info.setText('Въведи поне едно условие.');self.render_results([]);return
        rows=self.owner.db.search_known_parts(*fields,limit=48)
        self.info.setText(f'Локално: {len(rows)} резултата (до 48).')
        self.render_results(rows)

    def perform_online(self):
        if not self.owner.api.key:
            QMessageBox.information(self,'API ключ','За онлайн търсене въведи API ключ в Настройки. Офлайн търсенето и ръчното добавяне работят без него.')
            return
        terms=[self.number.text().strip(),self.description.text().strip(),self.shape.text().strip()]
        query=' '.join(x for x in terms if x).strip()
        if not query:
            QMessageBox.information(self,'Търсене','Въведи номер, описание или форма, за да търсиш онлайн.')
            return
        self.remote_serial+=1;serial=self.remote_serial
        self.search_online.setEnabled(False);self.info.setText('Търся в Rebrickable…')
        def work():
            result=self.owner.api.get('parts/',{'search':query,'page_size':45})
            return result.get('results') or []
        def done(data):
            if serial!=self.remote_serial:return
            self.search_online.setEnabled(True)
            rows=[]
            for item in data:
                number=str(item.get('part_num') or '').strip()
                if not number:continue
                rows.append({'part_num':number,'name':item.get('name') or number,'color_id':None,
                             'color':'Цвят се избира по-долу', 'image':item.get('part_img_url') or '', 'generic':True})
            self.info.setText(f'Онлайн: {len(rows)} резултата. Снимките са ориентировъчни до избор на цвят.')
            self.render_results(rows)
        def error(reason):
            if serial!=self.remote_serial:return
            self.search_online.setEnabled(True)
            self.info.setText('Онлайн търсенето не успя. Можеш да търсиш локално или да добавиш ръчно.')
            QMessageBox.warning(self,'Rebrickable',reason)
        self.owner.run_task(work,done,on_error=error)

    def render_results(self,rows):
        self.results=rows
        self.owner.clear_layout(self.result_grid)
        for i,entry in enumerate(rows):
            tile,v=panel_layout();tile.setMinimumWidth(175);tile.setMaximumWidth(265)
            v.addWidget(picture(self.owner.images,entry.get('image') or '',155,110),alignment=Qt.AlignCenter)
            name=str(entry.get('name') or entry.get('part_num') or '')
            v.addWidget(label(f'<b>#{html.escape(str(entry["part_num"]))}</b> · {html.escape(name[:52])}'))
            v.addWidget(label(str(entry.get('color') or 'Без указан цвят')[:34],muted=True))
            v.addWidget(make_button('Избери тази част',lambda checked=False,chosen=entry:self.pick(chosen),True))
            self.result_grid.addWidget(tile,i//4,i%4)
        for c in range(4):self.result_grid.setColumnStretch(c,1)
        self.scroller.verticalScrollBar().setValue(0)
        if not rows:self.result_grid.addWidget(label('Няма съвпадения. Пробвай по-общо описание или ръчно добавяне.'),0,0)

    def refresh_result_image(self,url):
        for item in self.result_host.findChildren(QLabel):
            if item.property('image_url')==url:
                pix=self.owner.images.get(url,(item.width()-8,item.height()-8))
                if not pix.isNull():item.setPixmap(pix)

    def disconnect_pictures(self,*_):
        try:self.owner.images.updated.disconnect(self.refresh_result_image)
        except (RuntimeError,TypeError):pass

    def pick(self,entry):
        self.selected=dict(entry)
        self.part_num.setText(str(entry['part_num']))
        self.part_name.setText(str(entry.get('name') or ''))
        if entry.get('color_id') is not None:
            self.color_id.setValue(int(entry['color_id']))
            self.color_name.setText(str(entry.get('color') or ''))
            self.presets.blockSignals(True);self.presets.setCurrentIndex(0);self.presets.blockSignals(False)
        self.pick_label.setText(f'Избрана: #{entry["part_num"]} · {str(entry.get("name") or "")[:65]}')
        self.update_stock_info()

    def save(self):
        num=self.part_num.text().strip()
        if not num or len(num)>100:
            QMessageBox.warning(self,'Номер','Въведи валиден номер на част (до 100 символа).');return
        cid=self.color_id.value()
        same=(self.selected is not None and self.selected.get('part_num')==num and
              self.selected.get('color_id') is not None and int(self.selected['color_id'])==cid)
        image=(self.selected.get('image') or '') if same else ''
        known=self.owner.db.row("SELECT image,name,color FROM parts WHERE part_num=? AND color_id=? AND image<>'' LIMIT 1",(num,cid))
        old=self.owner.db.row('SELECT * FROM loose WHERE part_num=? AND color_id=?',(num,cid))
        entry={'part_num':num,'color_id':cid,
               'name':self.part_name.text().strip() or (old or {}).get('name') or (known or {}).get('name') or num,
               'color':self.color_name.text().strip() or (old or {}).get('color') or (known or {}).get('color') or f'Цвят #{cid}',
               'image':(old or {}).get('image') or (known or {}).get('image') or image}
        try:
            total=self.owner.db.add_loose_quantity(entry,self.qty.value())
        except Exception as exc:
            QMessageBox.warning(self,'Неуспешно добавяне',str(exc));return
        self.owner.show_loose()
        self.owner.statusBar().showMessage(f'✓ Записани {self.qty.value()} бр. · общо свободни {total} бр.',6000)
        self.accept()
        QTimer.singleShot(500,self.owner.sync_loose_images)

class MainWindow(Features21, QMainWindow):
    def __init__(self):
        super().__init__();self.db=Store();self.api=Api();self.metrics=UsageMetrics(SETTINGS_PATH,PROGRAM_DIR);self.pool=QThreadPool(self);self.pool.setMaxThreadCount(4);self.images=Images(self.pool)
        self.images.updated.connect(self.refresh_pictures)
        self.current_set=None;self.search_results=[];self.current_parts=[];self.view_mode='Таблица';self.page=0;self.page_size=36
        try:
            shown_version=json.loads((PROGRAM_DIR/'version.json').read_text(encoding='utf8')).get('version','2.0.1')
        except (OSError,ValueError):
            shown_version='2.1.6'
        self.setWindowTitle('LEGO Collection Manager '+shown_version);self.setWindowIcon(QIcon(str(PROGRAM_DIR / 'collector_icon.ico')));self.resize(1450,900);self.setMinimumSize(840,600)
        self.feature_init();self.build_ui();self.show_sets()
        self.metrics.track("launch")
        # On first launch guide the user, but never lock access to offline sets.
        self.navigate(1 if self.api.key else 5)
        # Periodically try to resolve pictures of manually entered loose parts.
        # Network calls run on a worker; the SQLite connection stays on the UI thread.
        self.loose_sync_pending = False
        self.loose_retry_after = {}
        self.loose_sync_timer = QTimer(self)
        self.loose_sync_timer.setInterval(300_000)  # reconnection check every 5 minutes
        self.loose_sync_timer.timeout.connect(self.sync_loose_images)
        self.loose_sync_timer.start()
        QTimer.singleShot(4000, self.sync_loose_images)
        QTimer.singleShot(9500, self.check_update_silent)
    def build_ui(self):
        root=QWidget();root.setObjectName('root');self.setCentralWidget(root)
        outer=QHBoxLayout(root);outer.setContentsMargins(15,15,15,12);outer.setSpacing(18)
        sidebar=QFrame();sidebar.setObjectName('sidebar');sidebar.setFixedWidth(222)
        n=QVBoxLayout(sidebar);n.setContentsMargins(15,20,15,18);n.setSpacing(7)
        brand=QHBoxLayout();brand.setSpacing(11)
        mark=QLabel();mark.setObjectName('brandMark');mark.setFixedSize(42,42)
        mark.setPixmap(QIcon(str(PROGRAM_DIR/'collector_icon.ico')).pixmap(40,40))
        mark.setAlignment(Qt.AlignCenter);brand.addWidget(mark)
        brandtext=QVBoxLayout();brandtext.setSpacing(2)
        name=QLabel('COLLECTION');name.setObjectName('sidebarBrand');name.setStyleSheet('font-size:15px;font-weight:750;')
        brandtext.addWidget(name)
        sub=QLabel('LEGO · MANAGER');sub.setObjectName('sidebarNote');brandtext.addWidget(sub)
        brand.addLayout(brandtext,1);n.addLayout(brand)
        n.addSpacing(27)
        section=QLabel('КОЛЕКЦИЯ');section.setObjectName('sidebarNote');n.addWidget(section)
        n.addSpacing(5)
        entries=[('Търсене','search'),('Моите LEGO','collection'),('Липсващи части','missing'),
                 ('Свободни части','loose'),('Статистика','stats'),('Настройки','settings'),
                 ('Намерена част','reverse'),('Завърши комплект','finish')]
        self.nav_buttons=[];self.nav_icons=[]
        for i,(name,ico) in enumerate(entries):
            if i==5:
                n.addSpacing(16)
                cap=QLabel('ИНСТРУМЕНТИ');cap.setObjectName('sidebarNote');n.addWidget(cap)
                n.addSpacing(5)
            btn=make_button('   '+name,lambda checked=False,k=i:self.navigate(k))
            btn.setMinimumHeight(43);btn.setIcon(navigation_icon(ico));btn.setIconSize(QSize(21,21))
            btn.setObjectName('nav_idle');n.addWidget(btn)
            self.nav_buttons.append(btn);self.nav_icons.append(ico)
        n.addStretch()
        undo=make_button('   Отмени последното',self.undo);undo.setObjectName('nav_footer')
        undo.setIcon(navigation_icon('undo'));undo.setMinimumHeight(44)
        n.addWidget(undo)
        note=QLabel('Автоматично запазване');note.setObjectName('sidebarNote');n.addWidget(note)
        outer.addWidget(sidebar)
        self.stack=QStackedWidget();outer.addWidget(self.stack,1)
        self.make_search_page();self.make_sets_page();self.make_missing_page();self.make_loose_page();self.make_stats_page();self.make_settings_page();self.feature_make_pages()
        self.statusBar().showMessage(f'Локална база: {DB_PATH}')
        for area in self.findChildren(QAbstractScrollArea): enable_touch_scroll(area)
    def resizeEvent(self,event):
        super().resizeEvent(event)
        if not hasattr(self,'stack') or not self.isVisible():return
        if not hasattr(self,'responsive_timer'):
            self.responsive_timer=QTimer(self);self.responsive_timer.setSingleShot(True)
            self.responsive_timer.timeout.connect(self._responsive_refresh)
        self.responsive_timer.start(220)

    def _responsive_refresh(self):
        if self.stack.currentIndex()==1 and hasattr(self,'sets_grid'):
            self.show_sets()
        elif self.stack.currentIndex()==0 and getattr(self,'search_results',None):
            self.refresh_search_cards()

    def navigate(self,index):
        self.stack.setCurrentIndex(index)
        for i,b in enumerate(self.nav_buttons):
            b.setObjectName('nav_active' if index==i else 'nav_idle')
            b.setIcon(navigation_icon(self.nav_icons[i],i==index))
            b.style().unpolish(b);b.style().polish(b)
        if index==1:self.show_sets()
        if index==2:self.show_missing()
        if index==3:
            self.show_loose()
            if hasattr(self,'loose_sync_timer'):QTimer.singleShot(300,self.sync_loose_images)
        if index==4:self.show_stats()
        if index==7:self.feature_finish_suggestions()
    def make_search_page(self):
        page=QWidget();v=QVBoxLayout(page);v.addWidget(label('Търсене на LEGO комплект',heading=True))
        row=QHBoxLayout();self.search_entry=QLineEdit();self.search_entry.setPlaceholderText('Номер или име на комплект, напр. 75387')
        self.search_entry.returnPressed.connect(self.search);row.addWidget(self.search_entry,1);row.addWidget(make_button('Търси',self.search,True));v.addLayout(row)
        self.search_status=label('Въведи номер или име. За нови търсения е необходим интернет.',muted=True);v.addWidget(self.search_status)
        self.search_scroll=QScrollArea();self.search_scroll.setWidgetResizable(True);v.addWidget(self.search_scroll,1)
        self.search_host=QWidget();self.search_layout=QGridLayout(self.search_host);self.search_layout.setAlignment(Qt.AlignTop);self.search_scroll.setWidget(self.search_host);enable_touch_scroll(self.search_scroll)
        self.stack.addWidget(page)
    def make_sets_page(self):
        page=QWidget();v=QVBoxLayout(page)
        h=QHBoxLayout();h.addWidget(label('Моите LEGO',heading=True));h.addStretch()
        self.feature_set_header(h);h.addWidget(make_button('＋ Добави комплект',lambda:self.navigate(0),True));v.addLayout(h)
        self.sets_scroll=QScrollArea();self.sets_scroll.setWidgetResizable(True)
        self.sets_host=QWidget();self.sets_grid=QGridLayout(self.sets_host);self.sets_grid.setSpacing(15);self.sets_grid.setAlignment(Qt.AlignTop)
        self.sets_scroll.setWidget(self.sets_host);enable_touch_scroll(self.sets_scroll);v.addWidget(self.sets_scroll);self.stack.addWidget(page)
    def make_missing_page(self):
        page=QWidget();v=QVBoxLayout(page)
        v.addWidget(label('Общи липсващи части',heading=True))
        r=QHBoxLayout();self.missing_spares=QCheckBox('Покажи липсващите резервни части');self.missing_spares.toggled.connect(self.show_missing)
        r.addWidget(self.missing_spares);r.addStretch();r.addWidget(make_button('⇩ CSV за поръчка',self.feature_export_missing_csv));r.addWidget(make_button('Печат на липсите',lambda:self.print_missing(False)));v.addLayout(r)
        filters=QHBoxLayout();filters.setSpacing(7)
        self.missing_search=QLineEdit();self.missing_search.setPlaceholderText('Търси по име или номер на част')
        self.missing_search.setClearButtonEnabled(True);self.missing_search.textChanged.connect(self.apply_missing_filters)
        filters.addWidget(self.missing_search,3)
        self.missing_color=QComboBox();self.missing_color.addItem('Всички цветове')
        self.missing_color.currentTextChanged.connect(self.apply_missing_filters);filters.addWidget(self.missing_color,1)
        self.missing_size=QComboBox();self.missing_size.addItems(['Всички размери','1×1','1×2','1×3','1×4','2×2','2×3','2×4','2×6','Други'])
        self.missing_size.currentTextChanged.connect(self.apply_missing_filters);filters.addWidget(self.missing_size,1)
        self.missing_type=QComboBox();self.missing_type.addItems(['Всички видове','Plate','Brick','Tile','Slope','Други'])
        self.missing_type.currentTextChanged.connect(self.apply_missing_filters);filters.addWidget(self.missing_type,1)
        v.addLayout(filters)
        self.missing_result_count=label('',muted=True);v.addWidget(self.missing_result_count)
        self.missing_table=QTableView();self.missing_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.missing_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.missing_model=MissingModel(self.images,[])
        self.missing_table.setModel(self.missing_model);self.missing_table.doubleClicked.connect(self.open_missing)
        self.format_table(self.missing_table,96);self.missing_table.setItemDelegate(ImageButtonDelegate(self.missing_table,0));self.missing_table.setColumnWidth(0,136);self.missing_table.setColumnWidth(1,290)
        self.missing_table.setColumnWidth(2,130);self.missing_table.setColumnWidth(3,110)
        self.missing_table.setColumnWidth(4,375)
        self.missing_table.horizontalHeader().setSectionResizeMode(4,QHeaderView.Stretch)
        self.missing_delegate=MissingSourcesDelegate(self.missing_table)
        self.missing_table.setItemDelegateForColumn(4,self.missing_delegate)
        self.missing_table.viewport().installEventFilter(self)
        v.addWidget(self.missing_table)
        v.addWidget(label('Двоен клик върху реда → къде липсва частта и преместване между комплектите.',muted=True))
        self.stack.addWidget(page)
    def make_loose_page(self):
        page=QWidget();v=QVBoxLayout(page);v.addWidget(label('Свободни части',heading=True))
        v.addWidget(label('Части, които все още не са разпределени към LEGO комплект.',muted=True))
        r=QHBoxLayout()
        r.addWidget(make_button('＋ Намери / добави част',self.add_to_loose,True))
        self.feature_loose_buttons(r)
        r.addWidget(make_button('🗑 Изтрий избраната част',self.delete_selected_loose))
        r.addWidget(make_button('↻ Потърси липсващите снимки',lambda:self.sync_loose_images(force=True)))
        r.addStretch();v.addLayout(r)
        self.loose_table=QTableView();self.loose_model=LooseModel(self.images,[])
        self.loose_table.setModel(self.loose_model);self.loose_table.doubleClicked.connect(self.loose_details)
        self.loose_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.loose_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.format_table(self.loose_table,118);self.loose_table.setItemDelegate(ImageButtonDelegate(self.loose_table,0));self.loose_table.setColumnWidth(0,156);self.loose_table.setColumnWidth(1,250);v.addWidget(self.loose_table)
        v.addWidget(label('Двоен клик за преместване от свободния запас към комплект.',muted=True));self.stack.addWidget(page)
    def make_stats_page(self):
        page=QWidget();v=QVBoxLayout(page);v.addWidget(label('Статистика на колекцията',heading=True))
        self.stats_text=label();self.stats_text.setStyleSheet('font-size:20px;line-height:1.5;')
        card,c=panel_layout();c.addWidget(self.stats_text);v.addWidget(card);v.addStretch();self.stack.addWidget(page)
    def make_settings_page(self):
        page=QWidget();v=QVBoxLayout(page);v.setContentsMargins(16,12,16,20);v.setSpacing(12)
        v.addWidget(label('Настройки',heading=True))
        v.addWidget(label('Връзка с каталога, защита на колекцията и обновления.',muted=True))
        card,c=panel_layout()
        c.addWidget(label('Каталог Rebrickable',heading=False))
        c.addWidget(label('API ключът е необходим само за онлайн търсене. Вече добавените комплекти работят офлайн.',muted=True))
        info=label('Създай безплатен ключ от <a href="https://rebrickable.com/api/">Rebrickable API</a>.')
        info.setOpenExternalLinks(True);info.setTextFormat(Qt.RichText);c.addWidget(info)
        heading=label('Твоят API ключ');heading.setObjectName('fieldHeading');c.addWidget(heading)
        row=QHBoxLayout()
        self.key_entry=QLineEdit(self.api.key);self.key_entry.setPlaceholderText('Въведи Rebrickable API ключ')
        self.key_entry.setEchoMode(QLineEdit.Password);self.key_entry.setMinimumHeight(35)
        row.addWidget(self.key_entry,1)
        self.key_check_button=compact_action(make_button('Провери и запази',self.check_and_save_key,True))
        row.addWidget(self.key_check_button);c.addLayout(row)
        self.show_key_box=QCheckBox('Покажи ключа')
        self.show_key_box.toggled.connect(lambda shown:self.key_entry.setEchoMode(QLineEdit.Normal if shown else QLineEdit.Password))
        c.addWidget(self.show_key_box)
        self.key_status=label('Ключът е запазен на този компютър.' if self.api.key else 'Няма въведен ключ.',muted=True)
        c.addWidget(self.key_status)
        c.addWidget(label('Запазва се локално в data/settings.json. Не публикувай този файл.',muted=True))
        link=compact_action(make_button('Отвори „Моите LEGO“',lambda:self.navigate(1)))
        c.addWidget(link,0,Qt.AlignLeft)
        v.addWidget(card)
        card2,c2=panel_layout()
        c2.addWidget(label('Архивиране и офлайн снимки'))
        c2.addWidget(label('Резервното копие запазва всички записи. Офлайн подготовката изтегля оставащите снимки.',muted=True))
        buttons=QHBoxLayout()
        buttons.addWidget(compact_action(make_button('Създай резервно копие',self.do_backup)),0)
        self.offline_button=compact_action(make_button('Подготви за офлайн работа',self.prepare_offline))
        buttons.addWidget(self.offline_button,0);buttons.addStretch();c2.addLayout(buttons)
        self.offline_progress=QProgressBar();self.offline_progress.setRange(0,100);self.offline_progress.setValue(0);c2.addWidget(self.offline_progress)
        self.offline_status=label('Снимките се пазят в data/images.',muted=True);c2.addWidget(self.offline_status)
        pathlabel=label(f'Локална папка: {APP_DIR}',muted=True);pathlabel.setTextInteractionFlags(Qt.TextSelectableByMouse)
        c2.addWidget(pathlabel)
        v.addWidget(card2)
        card3,c3=panel_layout()
        c3.addWidget(label('Обновления'))
        c3.addWidget(label('Проверка през GitHub или инсталиране от проверен ZIP архив. Папката data не се подменя.',muted=True))
        c3.addWidget(compact_action(make_button('Провери за обновление',self.launch_updater)),0,Qt.AlignLeft)
        v.addWidget(card3)
        metrics_card,mc=panel_layout()
        mc.addWidget(label('Анонимна статистика за използването'))
        mc.addWidget(label('По желание: изпраща само броя стартирания, добавени комплекти и проверки на части. Не изпраща номера на LEGO, колекция, снимки или API ключ.',muted=True))
        self.metrics_consent=QCheckBox('Разрешавам изпращане на анонимна статистика')
        self.metrics_consent.setChecked(self.metrics.enabled)
        self.metrics_consent.toggled.connect(self.toggle_usage_metrics)
        mc.addWidget(self.metrics_consent)
        mc.addWidget(label('Може да откажеш по всяко време. Без свързана статистическа услуга нищо не се изпраща.',muted=True))
        v.addWidget(metrics_card)
        v.addStretch()
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(page);enable_touch_scroll(scroll)
        self.stack.addWidget(scroll)

    def toggle_usage_metrics(self, enabled):
        self.metrics.set_enabled(enabled)
        if enabled:
            self.metrics.track('consent_enabled')

    def launch_updater(self):
        updater=PROGRAM_DIR / 'LEGO Updater.exe'
        if not updater.is_file():
            QMessageBox.information(self,'Обновяване',
                'Updater-ът е наличен в portable EXE пакета, не в Python тестовия режим. '
                'Изгради пакета чрез BUILD_EXE.bat.')
            return
        try:
            subprocess.Popen([str(updater),'--pid',str(os.getpid())],cwd=str(PROGRAM_DIR))
        except OSError as e:
            QMessageBox.warning(self,'Обновяване',str(e))

    def check_update_silent(self):
        # Non-blocking, best-effort check. The user can continue offline.
        if not (PROGRAM_DIR/'LEGO Updater.exe').exists():return
        try:
            from update_core import github_latest, parse_version, read_version
        except ImportError:
            return
        def done(found):
            try:
                if parse_version(found[0])<=parse_version(read_version(PROGRAM_DIR)):return
                answer=QMessageBox.question(self,'Налична е нова версия',
                    f'Налична е версия {found[0]}. Да отворя прозореца за обновяване?')
                if answer==QMessageBox.Yes:self.launch_updater()
            except (ValueError,RuntimeError):pass
        # A failed check is intentionally silent (offline computers).
        self.run_task(lambda:github_latest(PROGRAM_DIR,timeout=7),done,on_error=lambda error:None)

    def check_and_save_key(self):
        candidate=self.key_entry.text().strip()
        if not candidate:
            self.key_status.setText('Постави ключа, за да го проверим.')
            return
        self.key_check_button.setEnabled(False)
        self.key_status.setText('Свързвам се с Rebrickable и проверявам ключа…')
        self.run_task(lambda: Api.check_key(candidate),self.key_verified,
                      on_error=self.key_verification_failed)

    def key_verified(self,candidate):
        self.api.save_key(candidate)
        self.key_check_button.setEnabled(True)
        self.key_status.setText('✓ Ключът е проверен и запазен. Можеш да търсиш LEGO комплекти.')
        self.statusBar().showMessage('API ключът е проверен успешно.',5000)
        QTimer.singleShot(300,self.sync_loose_images)

    def key_verification_failed(self,error):
        self.key_check_button.setEnabled(True)
        self.key_status.setText('⚠ '+error+' Предишният ключ, ако има такъв, не е променен.')

    def save_key(self):
        # Keep compatibility with older integrations: validation is required.
        self.check_and_save_key()

    def do_backup(self):
        self.db.backup();QMessageBox.information(self,'Резервно копие',f'Провери папката:\n{BACKUPS}')
    def prepare_offline(self):
        if getattr(self,'offline_task',None) is not None:
            return
        urls=set()
        for query in ('SELECT image FROM sets','SELECT image FROM parts','SELECT image FROM loose'):
            urls.update(r['image'] for r in self.db.rows(query) if r.get('image'))
        urls=sorted(u for u in urls if u.startswith('https://'))
        if not urls:
            QMessageBox.information(self,'Офлайн подготовка','Първо добави LEGO комплекти с картинки.')
            return
        # Image downloads already in flight are harmless: atomic cache-path replacement.
        self.offline_progress.setRange(0,len(urls))
        self.offline_progress.setValue(0)
        self.offline_status.setText(f'Проверявам {len(urls)} уникални снимки…')
        self.offline_button.setEnabled(False)
        task=OfflineImagesTask(urls)
        self.offline_task=task  # retain worker and signal objects until completion
        task.signals.progress.connect(self.offline_tick)
        task.signals.done.connect(self.offline_done)
        task.signals.error.connect(self.offline_error)
        self.pool.start(task)

    def offline_tick(self,done,total,failed):
        self.offline_progress.setValue(done)
        self.offline_status.setText(f'Проверени {done} / {total} снимки · неуспешни: {failed}')

    def offline_done(self,downloaded,failed,total):
        self.offline_task=None
        self.offline_button.setEnabled(True)
        msg=(f'Готово! Всички {total} снимки са налични локално.' if not failed
             else f'Проверени {total} снимки, но {failed} не успяха да се изтеглят. Натисни отново за повторен опит.')
        self.offline_status.setText(msg)
        self.statusBar().showMessage(msg,15000)
        QMessageBox.information(self,'Подготовка за офлайн работа',msg)
        # Redraw on next navigation; image cache is ready on disk.
        self.show_sets()

    def offline_error(self,reason):
        self.offline_task=None
        self.offline_button.setEnabled(True)
        self.offline_status.setText('Прекъсната подготовка: '+reason)
        QMessageBox.warning(self,'Офлайн подготовка',reason)

    def format_table(self,t,height):
        t.verticalHeader().setDefaultSectionSize(height);t.verticalHeader().hide();t.horizontalHeader().setStretchLastSection(True)
        t.setAlternatingRowColors(True);t.setSortingEnabled(False)
        t.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel);t.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        enable_touch_scroll(t)
    def run_task(self,fn,success,on_error=None):
        t=Task(fn)
        t.signals.success.connect(success)
        if on_error is None:
            t.signals.error.connect(lambda e:(self.statusBar().showMessage('Грешка: '+e,12000),QMessageBox.warning(self,'Грешка',e)))
        else:
            t.signals.error.connect(on_error)
        if not hasattr(self,'api_tasks'): self.api_tasks=set()
        self.api_tasks.add(t)
        t.signals.success.connect(lambda _,task=t:self.api_tasks.discard(task))
        t.signals.error.connect(lambda _,task=t:self.api_tasks.discard(task))
        self.pool.start(t)
    def search(self):
        q=self.search_entry.text().strip()
        if not q:return
        self.search_status.setText('Търся комплекти…')
        self.search_serial=getattr(self,'search_serial',0)+1
        serial=self.search_serial
        self.run_task(lambda:self.api.search(q),lambda rows:self.search_done(rows) if serial==self.search_serial else None)
    def search_done(self,rows):
        self.search_results=rows
        self.search_status.setText(f'Намерени: {len(rows)} резултата (показани до 100)')
        self.refresh_search_cards()

    def refresh_search_cards(self):
        """Show existing physical copies without prohibiting new copies."""
        if not hasattr(self,'search_results'):
            return
        self.clear_layout(self.search_layout)
        copies_by_number={}
        for item in self.db.all_sets():
            copies_by_number.setdefault(str(item['set_num']),[]).append(item)
        cols=max(1,min(4,max(300,self.search_scroll.viewport().width()-20)//325))
        for i,p in enumerate(self.search_results):
            f,v=panel_layout();f.setMinimumWidth(0)
            v.addWidget(picture(self.images,p.get('set_img_url',''),175,150),alignment=Qt.AlignHCenter)
            number=str(p.get('set_num',''))
            v.addWidget(label(f"<b>{html.escape(number)}</b> — {html.escape(str(p.get('name','')))}"))
            v.addWidget(label(f"{p.get('year','')} · {p.get('num_parts',0)} части",muted=True))
            copies=copies_by_number.get(number,[])
            if copies:
                v.addWidget(label(f'<b>✓ Този комплект вече е добавен в „Моите LEGO“ ({len(copies)} бр.).</b>'))
                for copy in copies:
                    group=QHBoxLayout()
                    state=('Непроверен' if not copy['started'] else
                           ('Комплектован' if copy['owned']>=copy['total'] else f"Липсват {copy['total']-copy['owned']}"))
                    group.addWidget(label(f"Екз. №{copy['id']} · {state}",muted=True),1)
                    group.addWidget(make_button('Отвори',lambda checked=False,id=copy['id']:self.open_set(id)))
                    delete_btn=make_button('Изтрий',lambda checked=False,id=copy['id']:self.confirm_delete_set(id))
                    delete_btn.setObjectName('danger')
                    group.addWidget(delete_btn)
                    v.addLayout(group)
            v.addWidget(make_button('＋ Добави още един екземпляр' if copies else
                                    '＋ Добави към моите LEGO',
                                    lambda checked=False,x=p:self.add_set(x),True))
            self.search_layout.addWidget(f,i//cols,i%cols)

    def add_set(self,entry):
        if not self.api.key:
            QMessageBox.warning(self,'API ключ','Първо въведи ключ в Настройки.');return
        self.search_status.setText('Изтеглям списъка с части (вкл. минифигурки)…')
        def finish(parts):
            if not parts:
                QMessageBox.warning(self,'Празен инвентар','API не върна части. Комплектът не е добавен.');return
            sid=self.db.add_set(entry,parts)
            self.metrics.track('set_added')
            self.feature_register_theme(sid,entry)
            self.search_status.setText(f'Добавен е комплект {entry["set_num"]} · {len(parts)} записа в API')
            self.show_sets();self.refresh_search_cards();self.open_set(sid)
        self.run_task(lambda:self.api.inventory(entry['set_num']),finish)
    @staticmethod
    def clear_layout(lay):
        while lay.count():
            item=lay.takeAt(0)
            if item.widget(): item.widget().deleteLater()
            elif item.layout(): MainWindow.clear_layout(item.layout())
    def show_sets(self):
        self.feature_show_sets()

    def confirm_delete_set(self,sid):
        entry=self.db.row('SELECT * FROM sets WHERE id=?',(sid,))
        if not entry:
            QMessageBox.warning(self,'Липсващ комплект','Този комплект вече не съществува.')
            self.show_sets()
            return
        duplicate_count=self.db.count('SELECT COUNT(*) FROM sets WHERE set_num=?',(entry['set_num'],))
        details=(f"<b>{html.escape(entry['name'])}</b><br>"
                 f"№ {html.escape(entry['set_num'])} · Екземпляр №{entry['id']}<br><br>"
                 'Ще изтрием само този конкретен екземпляр и неговите въведени наличности. '
                 'Свободните части и останалите комплекти остават непроменени.<br><br>'
                 'Преди изтриването ще се създаде резервно копие на базата.')
        if duplicate_count>1:
            details+=f'<br><br>Другите {duplicate_count-1} еднакви комплекта няма да бъдат изтрити.'
        box=QMessageBox(self)
        box.setWindowTitle('Потвърди изтриването')
        box.setIcon(QMessageBox.Warning)
        box.setText(details)
        confirm=box.addButton('Изтрий този екземпляр',QMessageBox.DestructiveRole)
        box.addButton('Отказ',QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not confirm:
            return
        try:
            candidate_images=self.db.image_urls_for_set(sid)
            backup_file=self.db.delete_set(sid)
        except (sqlite3.Error, OSError, ValueError) as exc:
            QMessageBox.critical(self,'Неуспешно изтриване',f'Комплектът не беше изтрит.\n{exc}')
            return
        removed_images=self.images.cleanup_unused(self.db, candidate_images)
        self.feature_cleanup_box_art(entry['set_num'])
        if self.current_set and self.current_set.get('id')==sid:
            if hasattr(self,'set_window') and self.set_window.isVisible():
                self.set_window.close()
            self.current_set=None
            self.current_parts=[]
        self.show_sets()
        self.refresh_search_cards()
        if self.stack.currentIndex()==2:self.show_missing()
        if self.stack.currentIndex()==4:self.show_stats()
        self.statusBar().showMessage(
            f'Комплектът е изтрит. Почистени снимки: {removed_images}. Резервно копие: {backup_file.name}',12000)

    def open_set(self,sid):
        self.current_set=self.db.row('SELECT * FROM sets WHERE id=?',(sid,))
        if not self.current_set:return
        if hasattr(self,'set_window') and self.set_window.isVisible():self.set_window.close()
        self.set_window=QDialog(self);self.set_window.setWindowTitle(f'{self.current_set["set_num"]} · {self.current_set["name"]}')
        self.set_window.setMinimumSize(760,560);self.set_window.resize(1240,780)
        v=QVBoxLayout(self.set_window);v.setContentsMargins(18,16,18,14);v.setSpacing(12)
        header=QHBoxLayout();header.addWidget(picture(self.images,self.current_set['image'],105,85))
        titlebox=QVBoxLayout();titlebox.addWidget(label(self.current_set['set_num'],muted=True));title=label(html.escape(self.current_set['name']));title.setObjectName('heading');title.setWordWrap(True);titlebox.addWidget(title)
        self.set_progress=label();titlebox.addWidget(self.set_progress);header.addLayout(titlebox,1)
        self.start_btn=make_button('▶ Започни проверка',self.start_check,True)
        header.addWidget(compact_action(self.start_btn));header.addWidget(compact_action(make_button('Нова проверка',self.reset_check)))
        header.addWidget(compact_action(make_button('Печат',self.print_set)))
        delete_btn=make_button('Изтрий комплект',lambda checked=False,id=sid:self.confirm_delete_set(id))
        delete_btn.setObjectName('danger')
        header.addWidget(compact_action(delete_btn))
        v.addLayout(header)
        tools=QHBoxLayout();self.set_filter=QLineEdit();self.set_filter.setPlaceholderText('Търси по име или номер на част')
        self.set_filter.textChanged.connect(self.render_parts);tools.addWidget(self.set_filter,2)
        self.color_filter=QComboBox();self.color_filter.currentIndexChanged.connect(self.render_parts);tools.addWidget(self.color_filter)
        self.state_filter=QComboBox();self.state_filter.addItems(['Всички части','Само липсващи','Само налични','Резервни части','Алтернативи'])
        self.state_filter.currentIndexChanged.connect(self.render_parts);tools.addWidget(self.state_filter)
        self.mode=QComboBox();self.mode.addItems(['Таблица','Карти','Книжка']);self.mode.setCurrentText(self.view_mode)
        self.mode.currentTextChanged.connect(self.set_view_mode);tools.addWidget(self.mode)
        tools.addWidget(label('Размер:',muted=True))
        self.image_slider=QSlider(Qt.Horizontal);self.image_slider.setRange(85,220);self.image_slider.setValue(getattr(self,'image_size',155));self.image_slider.setFixedWidth(95)
        self.image_slider.valueChanged.connect(self.resize_part_images);tools.addWidget(self.image_slider)
        v.addLayout(tools)
        self.part_container=QStackedWidget();v.addWidget(self.part_container,1)
        self.part_model=PartModel(self)
        self.part_table=QTableView();self.part_table.setModel(self.part_model);enable_touch_scroll(self.part_table)
        self.part_table.setItemDelegate(ImageButtonDelegate(self.part_table,0,(7,8,9)))
        self.part_table.setIconSize(QSize(155,125))
        self.format_table(self.part_table,148);self.part_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.Interactive)
        self.part_table.setColumnWidth(0,200);self.part_table.setColumnWidth(1,215);self.part_table.setColumnWidth(2,125)
        for col in (7,8,9):self.part_table.setColumnWidth(col,50)
        self.part_table.doubleClicked.connect(self.table_clicked)
        self.part_table.clicked.connect(self.table_action)
        self.part_table.setContextMenuPolicy(Qt.CustomContextMenu);self.part_table.customContextMenuRequested.connect(self.part_menu)
        self.part_container.addWidget(self.part_table);self.resize_part_images(self.image_slider.value())
        self.cards_scroller=QScrollArea();self.cards_scroller.setWidgetResizable(True)
        self.cards_host=QWidget();self.cards_grid=QGridLayout(self.cards_host);self.cards_grid.setAlignment(Qt.AlignTop)
        self.cards_scroller.setWidget(self.cards_host);enable_touch_scroll(self.cards_scroller);self.part_container.addWidget(self.cards_scroller)
        self.cards_scroller.verticalScrollBar().valueChanged.connect(self.schedule_visible_images)
        self.cards_scroller.viewport().installEventFilter(self)
        self.book_scroller=QScrollArea();self.book_scroller.setWidgetResizable(True)
        self.book_host=QWidget();self.book_grid=QGridLayout(self.book_host);self.book_grid.setAlignment(Qt.AlignTop)
        self.book_scroller.setWidget(self.book_host);enable_touch_scroll(self.book_scroller);self.part_container.addWidget(self.book_scroller)
        self.book_scroller.verticalScrollBar().valueChanged.connect(self.schedule_visible_images)
        self.book_scroller.viewport().installEventFilter(self)
        bottom=QHBoxLayout();self.page_info=label();bottom.addWidget(self.page_info);bottom.addStretch()
        self.prev_btn=make_button('← Предишна',lambda:self.change_page(-1));self.next_btn=make_button('Следваща →',lambda:self.change_page(1))
        bottom.addWidget(self.prev_btn);bottom.addWidget(self.next_btn);v.addLayout(bottom)
        self.load_set_parts();self.set_window.show();self.schedule_visible_images()
    def load_set_parts(self):
        self.current_set=self.db.row('SELECT * FROM sets WHERE id=?',(self.current_set['id'],))
        self.current_parts=self.db.parts_for(self.current_set['id'])
        colors=sorted(set(p['color'] for p in self.current_parts))
        chosen=self.color_filter.currentText() if hasattr(self,'color_filter') else 'Всички цветове'
        self.color_filter.blockSignals(True);self.color_filter.clear();self.color_filter.addItem('Всички цветове');self.color_filter.addItems(colors)
        self.color_filter.setCurrentText(chosen if chosen in colors else 'Всички цветове');self.color_filter.blockSignals(False)
        self.start_btn.setVisible(not self.current_set['started'])
        mandatory=[p for p in self.current_parts if not p['spare'] and not p['alternate']]
        owned=sum(p['owned'] for p in mandatory);total=sum(p['required'] for p in mandatory)
        text=(f'Налични: {owned} / {total} · Липсват: {total-owned}' if self.current_set['started'] else 'Проверката още не е започнала.')
        self.set_progress.setText(text)
        self.render_parts()
    def start_check(self):
        self.db.start(self.current_set['id']);self.load_set_parts();self.on_data_changed(False)
    def reset_check(self):
        if QMessageBox.question(self,'Нова проверка','Нова проверка ще занули всички наличности за комплекта. Продължаваме?')!=QMessageBox.Yes:return
        self.db.backup()
        with self.db.db:
            self.db.db.execute('UPDATE parts SET owned=0 WHERE set_id=?',(self.current_set['id'],))
            self.db.db.execute('UPDATE sets SET started=1 WHERE id=?',(self.current_set['id'],))
        self.db.undo=None;self.load_set_parts();self.on_data_changed(False)
    def filtered_parts(self):
        q=self.set_filter.text().strip().casefold();color=self.color_filter.currentText();state=self.state_filter.currentText()
        filtered=[]
        for p in self.current_parts:
            if q and q not in (p['name']+' '+p['part_num']+' '+p['color']).casefold():continue
            if color!='Всички цветове' and p['color']!=color:continue
            if state=='Само липсващи' and (p['required']<=p['owned'] or p['alternate']):continue
            if state=='Само налични' and (p['owned']==0 or p['alternate']):continue
            if state=='Резервни части' and not p['spare']:continue
            if state=='Алтернативи' and not p['alternate']:continue
            if state in ('Всички части','Само липсващи','Само налични') and p['alternate']:continue
            filtered.append(p)
        return filtered
    def resize_part_images(self,n):
        self.image_size=n
        if hasattr(self,'part_table'):
            self.part_table.setColumnWidth(0,n+35)
            self.part_table.verticalHeader().setDefaultSectionSize(n+18)
            self.part_table.setIconSize(QSize(n,n))
        if hasattr(self,'mode') and self.mode.currentText()!='Таблица':self.render_parts()
    def set_view_mode(self,mode):self.view_mode=mode;self.page=0;self.render_parts()
    def change_page(self,offset):self.page+=offset;self.render_parts()
    def render_parts(self):
        if not hasattr(self,'part_container'):return
        all_parts=sort_inventory_parts(self.filtered_parts(),self.mode.currentText())
        maxpage=max(0,(len(all_parts)-1)//self.page_size);self.page=max(0,min(self.page,maxpage))
        selected=all_parts[self.page*self.page_size:(self.page+1)*self.page_size]
        self.page_info.setText(f'Страница {self.page+1}/{maxpage+1} · {len(selected)} показани · Общо {len(all_parts)} вида части')
        self.prev_btn.setEnabled(self.page>0);self.next_btn.setEnabled(self.page<maxpage)
        self.part_model.set_items(selected)
        mode=self.mode.currentText();self.part_container.setCurrentIndex(['Таблица','Карти','Книжка'].index(mode))
        if mode=='Таблица':return
        lay=self.cards_grid if mode=='Карти' else self.book_grid
        self.clear_layout(lay)
        scroller=self.cards_scroller if mode=='Карти' else self.book_scroller
        available=max(350,scroller.viewport().width()-34)
        minimum=260 if mode=='Карти' else 175
        cols=max(1,min(5,available//minimum))
        place_row=0;place_col=0;last_color=None;last_spare=None
        for i,p in enumerate(selected):
            if mode=='Книжка' and p['spare']!=last_spare:
                if place_col:place_row+=1;place_col=0
                section=label('Резервни части' if p['spare'] else 'Основни части')
                section.setObjectName('sectionHeading')
                lay.addWidget(section,place_row,0,1,cols)
                place_row+=1;last_color=None;last_spare=p['spare']
            if mode=='Книжка' and p['color']!=last_color:
                if place_col:place_row+=1;place_col=0
                heading=label(f'{p["color"]}  ·  {sum(x["color"]==p["color"] for x in all_parts)} вида части')
                heading.setObjectName('sectionHeading')
                lay.addWidget(heading,place_row,0,1,cols);place_row+=1
                last_color=p['color']
            f,fl=panel_layout();f.setObjectName('inventoryCard')
            fl.setContentsMargins(10,9,10,9);fl.setSpacing(5)
            f.setMinimumWidth(0)
            if mode=='Книжка':
                pic=picture(self.images,p['image'],min(155,self.image_slider.value()+10),min(125,self.image_slider.value()),lazy=True)
                fl.addWidget(pic,alignment=Qt.AlignCenter)
                fl.addWidget(label(f'<b>{p["required"]}×</b>  ·  #{html.escape(p["part_num"])}'),alignment=Qt.AlignCenter)
            else:
                fl.addWidget(picture(self.images,p['image'],min(170,self.image_slider.value()+20),min(145,self.image_slider.value()+5),lazy=True),alignment=Qt.AlignCenter)
                fl.addWidget(label(f'<b>{html.escape(p["name"][:46])}</b>'))
                fl.addWidget(label(f'#{html.escape(p["part_num"])} · {html.escape(p["color"])}',muted=True))
            if p['spare']:fl.addWidget(label('Резервна част',muted=True))
            controls=QHBoxLayout();controls.setSpacing(5)
            minus=make_button('−');plus=make_button('+')
            compact_action(minus,31);compact_action(plus,31)
            qty=QSpinBox();qty.setRange(0,int(p['required']));qty.setValue(int(p['owned']))
            qty.setButtonSymbols(QSpinBox.NoButtons);qty.setKeyboardTracking(False)
            qty.setAlignment(Qt.AlignCenter);qty.setFixedWidth(70);qty.setFixedHeight(32)
            # Explicit palette and editor CSS: on Windows some native QSpinBox themes
            # paint white text over a white background without this override.
            qty.setStyleSheet('QSpinBox {background:#FFFFFF; color:#1D2936; border:1px solid #B9C6D2; border-radius:6px; padding:2px; font-size:13px; font-weight:650;} QSpinBox:disabled {color:#68788B;background:#F3F5F7;}')
            qty.lineEdit().setStyleSheet('color:#1D2936;background:transparent;border:0px;font-size:13px;font-weight:650;')
            qty.setToolTip('Налични бройки (можеш да въведеш число)')
            controls.addStretch();controls.addWidget(minus);controls.addWidget(qty);controls.addWidget(plus);controls.addStretch()
            fl.addLayout(controls)
            checked=QCheckBox('Всички налични')
            checked.setChecked(p['owned']>=p['required']);fl.addWidget(checked)
            info=label('');fl.addWidget(info)
            def update_info(part=p,box=checked,indicator=info):
                remaining=max(0,part['required']-part['owned'])
                indicator.setText(f'✓ Налични {part["owned"]}/{part["required"]}' if remaining==0 else f'Липсват: {remaining} бр.  ·  Налични: {part["owned"]}/{part["required"]}')
                indicator.setStyleSheet('color:#22704d;font-weight:700;' if remaining==0 else 'color:#b34b50;font-weight:700;')
                box.blockSignals(True);box.setChecked(remaining==0);box.blockSignals(False)
            update_info()
            can_edit=bool(self.current_set['started'] and not p['alternate'])
            for ctl in (minus,plus,qty,checked):ctl.setEnabled(can_edit)
            if not can_edit:
                info.setToolTip('Започни проверка, за да отбелязваш наличните части.' if not self.current_set['started'] else 'Алтернативите не се броят отделно.')
            def change_count(value,part=p,indicator_update=update_info):
                value=max(0,min(int(value),int(part['required'])))
                if value==int(part['owned']):return
                self.db.set_owned(part['id'],value)
                self.metrics.track('part_checked')
                part['owned']=value
                indicator_update()
                for row,part_ref in enumerate(self.part_model.items):
                    if part_ref['id']==part['id']:
                        self.part_model.dataChanged.emit(self.part_model.index(row,4),self.part_model.index(row,9))
                        break
                self.on_data_changed(False)
            qty.valueChanged.connect(change_count)
            minus.clicked.connect(lambda _checked=False,s=qty:s.setValue(s.value()-1))
            plus.clicked.connect(lambda _checked=False,s=qty:s.setValue(s.value()+1))
            checked.toggled.connect(lambda value,s=qty,n=p['required']:s.setValue(n if value else 0))
            if mode=='Карти':
                fl.addWidget(compact_action(make_button('Подробности / Премести',lambda _checked=False,part=p:self.edit_part(part))))
            if p['owned']>=p['required'] and self.current_set['started']:
                f.setProperty('isComplete','true')
                f.style().unpolish(f);f.style().polish(f)
            lay.addWidget(f,place_row,place_col)
            place_col+=1
            if place_col>=cols:place_col=0;place_row+=1
        for i in range(cols):lay.setColumnStretch(i,1)
        self.schedule_visible_images()

    def eventFilter(self,obj,event):
        if event.type()==QEvent.Resize and hasattr(self,'missing_table') and obj is self.missing_table.viewport():
            if not hasattr(self,'missing_resize_timer'):
                self.missing_resize_timer=QTimer(self);self.missing_resize_timer.setSingleShot(True)
                self.missing_resize_timer.timeout.connect(self.update_missing_row_heights)
            self.missing_resize_timer.start(120)
        if event.type()==QEvent.Resize and hasattr(self,'part_container') and hasattr(self,'set_window') and self.set_window.isVisible():
            if obj in (self.cards_scroller.viewport(),self.book_scroller.viewport()):
                if not hasattr(self,'resize_grid_timer'):
                    self.resize_grid_timer=QTimer(self);self.resize_grid_timer.setSingleShot(True)
                    self.resize_grid_timer.timeout.connect(self.render_parts)
                self.resize_grid_timer.start(180)
        return super().eventFilter(obj,event)

    def schedule_visible_images(self,*args):
        # Coalesce rapid scroll events, and request only pictures inside the viewport.
        if not hasattr(self,'visible_image_timer'):
            self.visible_image_timer=QTimer(self)
            self.visible_image_timer.setSingleShot(True)
            self.visible_image_timer.timeout.connect(self.load_visible_images)
        self.visible_image_timer.start(60)

    def load_visible_images(self):
        if not hasattr(self,'mode') or not hasattr(self,'set_window') or not self.set_window.isVisible():
            return
        mode=self.mode.currentText()
        if mode not in ('Карти','Книжка'):return
        scroller=self.cards_scroller if mode=='Карти' else self.book_scroller
        if not scroller.isVisible():return
        viewport=scroller.viewport()
        for pic in scroller.findChildren(QLabel):
            if not pic.property('lazy_image_pending'):continue
            pos=pic.mapTo(viewport,QPoint(0,0))
            # Add a little prefetched space above/below for smooth scrolling.
            if pos.y()>viewport.height()+230 or pos.y()+pic.height()<-230:continue
            if pos.x()>viewport.width()+80 or pos.x()+pic.width()<-80:continue
            pic.setProperty('lazy_image_pending',False)
            url=pic.property('image_url') or ''
            pm=self.images.get(url,(pic.width()-8,pic.height()-8))
            if not pm.isNull():pic.setPixmap(pm)
            else:pic.setText('·')

    def edit_part(self,p):
        if not self.current_set['started']:
            QMessageBox.information(self,'Първо започни проверката','Натисни „Започни проверка“.');return
        if p['alternate']:
            QMessageBox.information(self,'Алтернативна част','Това е алтернативен вариант. Проверявай основната част, за да не се брои двойно.');return
        QuantityDialog(self,self.db,p,self.images,lambda:self.on_data_changed(True)).exec()
    def table_action(self,idx):
        if not idx.isValid() or idx.column() not in (7,8,9):return
        p=self.part_model.items[idx.row()]
        if not self.current_set['started'] or p['alternate']:
            QMessageBox.information(self,'Проверка','Първо започни проверката; алтернативите не се броят отделно.');return
        n=p['owned']+(-1 if idx.column()==7 else (1 if idx.column()==8 else p['required']-p['owned']))
        self.db.set_owned(p['id'],n);self.metrics.track('part_checked');p['owned']=max(0,min(p['required'],n))
        self.part_model.dataChanged.emit(self.part_model.index(idx.row(),4),self.part_model.index(idx.row(),9))
        self.on_data_changed(False)
    def table_clicked(self,idx):
        if idx.isValid() and idx.column() not in (4,7,8,9):self.edit_part(self.part_model.items[idx.row()])
    def part_menu(self,point):
        idx=self.part_table.indexAt(point)
        if not idx.isValid():return
        p=self.part_model.items[idx.row()];menu=QMenu(self)
        menu.addAction('Редактирай наличните',lambda:self.edit_part(p))
        menu.addAction('В кои комплекти се използва?',lambda:self.part_uses(p))
        menu.addAction('Премести част',lambda:self.open_transfer(p))
        menu.exec(self.part_table.viewport().mapToGlobal(point))
    def part_uses(self,p):
        rows=self.db.where_used(p['part_num'],p['color_id'])
        QMessageBox.information(self,'Къде се използва?','\n'.join(f'{x["set_num"]} {x["set_name"]}: {x["owned"]}/{x["required"]}' for x in rows))
    def open_transfer(self,p):
        if TransferDialog(self,self.db,p['part_num'],p['color_id'],p.get('id')).exec():self.on_data_changed(True)
    def on_data_changed(self,refresh_parts=True):
        if refresh_parts and self.current_set and hasattr(self,'set_window') and self.set_window.isVisible():self.load_set_parts()
        elif self.current_set and hasattr(self,'set_window') and self.set_window.isVisible():
            s=self.db.row('''SELECT SUM(CASE WHEN spare=0 AND alternate=0 THEN required ELSE 0 END) total,
                              SUM(CASE WHEN spare=0 AND alternate=0 THEN owned ELSE 0 END) owned FROM parts WHERE set_id=?''',(self.current_set['id'],))
            self.set_progress.setText(f'Налични: {s["owned"] or 0} / {s["total"] or 0} · Липсват: {(s["total"] or 0)-(s["owned"] or 0)}')
        self.statusBar().showMessage('✓ Промените са запазени',3000)
    def show_missing(self):
        self.all_missing_data=self.db.missing(self.missing_spares.isChecked())
        chosen=self.missing_color.currentText()
        colors=sorted({p['color'] for p in self.all_missing_data},key=str.casefold)
        self.missing_color.blockSignals(True);self.missing_color.clear();self.missing_color.addItem('Всички цветове')
        self.missing_color.addItems(colors)
        if chosen in colors:self.missing_color.setCurrentText(chosen)
        self.missing_color.blockSignals(False)
        self.apply_missing_filters()

    def apply_missing_filters(self,*_args):
        if not hasattr(self,'all_missing_data'):return
        query=self.missing_search.text().casefold().strip()
        color=self.missing_color.currentText();size=self.missing_size.currentText();typ=self.missing_type.currentText()
        result=[]
        for p in self.all_missing_data:
            name=p['name'];txt=(name+' '+p['part_num']).casefold()
            if query and query not in txt:continue
            if color!='Всички цветове' and color!=p['color']:continue
            dims=re.search(r'(\d+)\s*[x×]\s*(\d+)',name,re.I)
            dim=('×'.join(sorted(dims.groups(),key=int)) if dims else '')
            if size!='Всички размери' and (dim!=size if size!='Други' else bool(dim and dim in {'1×1','1×2','1×3','1×4','2×2','2×3','2×4','2×6'})):continue
            if typ!='Всички видове' and (not name.casefold().startswith(typ.casefold()) if typ!='Други' else name.casefold().startswith(('plate','brick','tile','slope'))):continue
            result.append(p)
        # Always recompute the catalog order after filtering and after a stock update.
        # This is independent of SQLite's former 'most missing first' order.
        result=sort_missing_parts(result)
        self.missing_data=result
        # Use actual per-set missing quantities, not a detached aggregate count.
        # Include each owned instance separately and only the selected spare class.
        source_rows=self.db.rows("""SELECT p.part_num,p.color_id,p.set_id,s.set_num,
                    s.image AS image,SUM(p.required-p.owned) AS missing
                FROM parts p JOIN sets s ON s.id=p.set_id
                WHERE s.started=1 AND p.alternate=0 AND p.spare=? AND p.required>p.owned
                GROUP BY p.part_num,p.color_id,p.set_id,s.set_num,s.image
                ORDER BY s.set_num,s.id""",(int(self.missing_spares.isChecked()),))
        sources={}
        for r in source_rows:
            key=(r['part_num'],r['color_id'])
            sources.setdefault(key,[]).append(dict(r))
        self.missing_model.set_sources=sources
        self.missing_model.set_rows([['',f'{p["name"][:70]}\n#{p["part_num"]}',p['color'],p['missing'],''] for p in result],result)
        self.update_missing_row_heights()
        self.missing_result_count.setText(f'Показани {len(result)} от {len(self.all_missing_data)} вида липсващи части')
    def update_missing_row_heights(self):
        if not hasattr(self,'missing_table') or not hasattr(self,'missing_data'):
            return
        width=max(110,self.missing_table.columnWidth(4))
        delegate=self.missing_delegate
        for row in range(len(self.missing_data)):
            count=len(self.missing_model.sources_for(row))
            target=delegate.row_height(count,width)
            if self.missing_table.rowHeight(row)!=target:
                self.missing_table.setRowHeight(row,target)

    def open_missing(self,idx):
        if not idx.isValid():return
        p=self.missing_data[idx.row()];rows=self.db.where_used(p['part_num'],p['color_id'],True,self.missing_spares.isChecked())
        dlg=QDialog(self);dlg.setWindowTitle('Къде липсва тази част?');dlg.resize(760,560)
        v=QVBoxLayout(dlg);h=QHBoxLayout();h.addWidget(picture(self.images,p['image'],120,110))
        h.addWidget(label(f"<b>{html.escape(p['name'])}</b><br>#{p['part_num']} · {html.escape(p['color'])}<br>Общо липсват: {p['missing']}"));v.addLayout(h)
        scroller=QScrollArea();scroller.setWidgetResizable(True);enable_touch_scroll(scroller)
        host=QWidget();items=QVBoxLayout(host)
        for x in rows:
            card,body=panel_layout();line=QHBoxLayout()
            record=self.db.row('SELECT image FROM sets WHERE id=?',(x['set_id'],)) or {}
            line.addWidget(picture(self.images,record.get('image',''),112,90))
            info=QVBoxLayout()
            info.addWidget(label(f"<b>{html.escape(x['set_num'])} · {html.escape(x['set_name'])}</b>"))
            info.addWidget(label(f"Липсват: {x['required']-x['owned']} · Налични: {x['owned']}/{x['required']}",muted=True))
            line.addLayout(info,1)
            line.addWidget(make_button('Отвори',lambda checked=False,r=x:(dlg.accept(),self.open_set(r['set_id']))))
            body.addLayout(line);items.addWidget(card)
        items.addStretch();scroller.setWidget(host);v.addWidget(scroller,1)
        v.addWidget(make_button('↔ Разпредели / премести тази част',lambda:(dlg.accept(),self.transfer_from_missing(p)),True))
        v.addStretch();dlg.exec()
    def transfer_from_missing(self,p):
        if TransferDialog(self,self.db,p['part_num'],p['color_id']).exec():self.show_missing();self.on_data_changed()
    def show_loose(self):
        self.loose_data=self.db.loose_rows()
        rows=[]
        for r in self.loose_data:
            title=r['name'][:42] + ('…' if len(r['name'])>42 else '')
            rows.append(['', f"{title}\n#{r['part_num']}", r['color'], r['quantity']])
        self.loose_model.set_rows(rows, self.loose_data)
    def loose_details(self,idx):
        if not idx.isValid():return
        p=self.loose_data[idx.row()]
        if TransferDialog(self,self.db,p['part_num'],p['color_id']).exec():self.show_loose();self.on_data_changed()
    def add_to_loose(self):
        LoosePartSearchDialog(self).exec()

    def add_loose_manually(self):
        # Compatibility with older buttons, should any remain.
        self.add_to_loose()

    def delete_selected_loose(self):
        selected=self.loose_table.selectionModel().selectedRows()
        if not selected:
            QMessageBox.information(self,'Изтриване','Първо маркирай ред от свободните части.');return
        i=selected[0].row()
        if i>=len(self.loose_data):return
        p=self.loose_data[i]
        answer=QMessageBox.question(self,'Изтриване на свободна част',
            f'Да изтрия ли {p["quantity"]} бр. свободни части?\n\n{p["name"]}\n#{p["part_num"]} · {p["color"]}\n\nЧастите в комплектите няма да бъдат засегнати.',
            QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if answer!=QMessageBox.Yes:return
        removed=self.db.delete_loose(p['part_num'],p['color_id'])
        if removed and removed.get('image'):
            self.images.cleanup_unused(self.db,{removed['image']})
        self.show_loose();self.on_data_changed(False)

    def sync_loose_images(self,force=False):
        """Resolve picture URLs asynchronously when a connection is available.

        Never access SQLite from a worker. Reuse existing picture URLs first.
        """
        if getattr(self,'loose_sync_pending',False):
            return
        missing=self.db.rows("SELECT part_num,color_id FROM loose WHERE quantity>0 AND (image IS NULL OR image='') ORDER BY part_num")
        if not missing:
            return
        now=time.monotonic()
        candidate=next((r for r in missing if force or self.loose_retry_after.get((r['part_num'],r['color_id']),0)<now),None)
        if not candidate:
            return
        num,cid=candidate['part_num'],candidate['color_id']
        # Same part+color from a known set can supply the exact picture offline.
        known=self.db.row("SELECT image FROM parts WHERE part_num=? AND color_id=? AND image<>'' LIMIT 1",(num,cid))
        if known:
            with self.db.db:
                self.db.db.execute("UPDATE loose SET image=? WHERE part_num=? AND color_id=? AND (image IS NULL OR image='')",(known['image'],num,cid))
            self.show_loose()
            QTimer.singleShot(100,self.sync_loose_images)
            return
        if not self.api.key:
            return
        self.loose_sync_pending=True
        def fetch():
            response=self.api.get(f'parts/{quote(num,safe="")}/colors/{cid}/')
            return str(response.get('part_img_url') or '')
        def done(url):
            self.loose_sync_pending=False
            if url.startswith('https://'):
                with self.db.db:
                    self.db.db.execute("UPDATE loose SET image=? WHERE part_num=? AND color_id=? AND (image IS NULL OR image='')",(url,num,cid))
                self.show_loose()
                self.statusBar().showMessage('✓ Намерена снимка за свободна част',5000)
            else:
                self.loose_retry_after[(num,cid)]=time.monotonic()+1800
            # Slow down bursts; a single API query at a time, to respect rate limits.
            QTimer.singleShot(1200,self.sync_loose_images)
        def failed(error):
            self.loose_sync_pending=False
            self.loose_retry_after[(num,cid)]=time.monotonic()+1800
            # No popups for offline/404/network errors. Manual quantities are safe.
            self.statusBar().showMessage('Снимката ще се потърси при следваща връзка.',4500)
        self.run_task(fetch,done,on_error=failed)

    def show_stats(self):
        s=self.db.all_sets();active=[p for p in s if p['started']]
        missing=sum(max(0,p['total']-p['owned']) for p in active)
        completed=sum(1 for p in active if p['total']<=p['owned'])
        loose=sum(p['quantity'] for p in self.db.loose_rows())
        self.stats_text.setText(f'Комплекти: {len(s)}\n\nЗапочнати проверки: {len(active)}\n\nНапълно комплектовани: {completed}\n\nЛипсващи задължителни части: {missing}\n\nСвободни части: {loose}')
    def undo(self):
        if self.db.undo_last():
            self.on_data_changed(True);self.navigate(self.stack.currentIndex())
        else:QMessageBox.information(self,'Отмяна','Няма действие за отмяна в тази сесия.')
    def refresh_pictures(self,url):
        # Update currently visible pictures; table icon cache too.
        image_roots=[self.search_host,self.sets_host]
        for name in ('rev_host','finish_host'):
            if hasattr(self,name):image_roots.append(getattr(self,name))
        if hasattr(self,'set_window') and self.set_window.isVisible():image_roots.append(self.set_window)
        for root in image_roots:
            for pic in root.findChildren(QLabel):
                if pic.property('image_url')==url and not pic.property('lazy_image_pending'):
                    pm=self.images.get(url,(pic.width()-8,pic.height()-8))
                    if not pm.isNull():pic.setPixmap(pm)
        if hasattr(self,'missing_model') and getattr(self.missing_model,'parts',None):
            for i,item in enumerate(self.missing_model.parts):
                if item.get('image')==url:
                    ix=self.missing_model.index(i,0)
                    self.missing_model.dataChanged.emit(ix,ix)
        if hasattr(self,'missing_model') and getattr(self.missing_model,'parts',None):
            sources=getattr(self.missing_model,'set_sources',{})
            for i,item in enumerate(self.missing_model.parts):
                if any(source.get('image')==url for source in sources.get((item['part_num'],item['color_id']),[])):
                    ix=self.missing_model.index(i,4)
                    self.missing_model.dataChanged.emit(ix,ix)
        if hasattr(self,'part_model') and self.part_model.rowCount():
            for i,p in enumerate(self.part_model.items):
                if p['image']==url:
                    ix=self.part_model.index(i,0)
                    self.part_model.dataChanged.emit(ix,ix)
        if hasattr(self,'loose_model') and getattr(self.loose_model,'parts',None):
            for i,item in enumerate(self.loose_model.parts):
                if item.get('image')==url:
                    ix=self.loose_model.index(i,0)
                    self.loose_model.dataChanged.emit(ix,ix)
    def print_set(self):
        if not self.current_set:return
        opts=['Само липсващите части','Всички части']
        choice,ok=QInputDialog.getItem(self,'Печат','Какво да отпечатаме?',opts,0,False)
        if not ok:return
        parts=self.db.parts_for(self.current_set['id']);missing=(choice==opts[0])
        if missing:parts=[p for p in parts if self.current_set['started'] and p['required']>p['owned'] and not p['alternate']]
        self.print_parts(parts,self.current_set['set_num']+' '+self.current_set['name'],missing)
    def print_missing(self,from_set):
        parts=[]
        for p in self.db.missing(False):parts.append(dict(p,required=p['missing'],owned=0,spare=0))
        self.print_parts(parts,'Общо липсващи части',True)
    def print_parts(self,parts,title,missing):
        if not parts:
            QMessageBox.information(self,'Печат','Няма части за отпечатване.');return
        # Local images embedded as base64 data URIs for PDF/print support.
        import base64
        tiles=[]
        for p in parts:
            url=p.get('image') or '';pix=self.images.get(url,(120,105));img=''
            if not pix.isNull():
                from PySide6.QtCore import QByteArray,QBuffer,QIODevice
                ba=QByteArray();buf=QBuffer(ba);buf.open(QIODevice.WriteOnly);pix.save(buf,'PNG');buf.close()
                img='<img width="92" height="90" src="data:image/png;base64,'+bytes(ba.toBase64()).decode()+'">'
            amount=p.get('missing',max(0,p.get('required',0)-p.get('owned',0))) if missing else p['required']
            tiles.append(f'<td width="24%" height="155" align="center" valign="top">{img}<br><b>{int(amount)}×</b><br><span style="font-size:8pt">{html.escape(p["part_num"])} · {html.escape(p.get("color") or "")}</span></td>')
        rows=['<tr>'+''.join(tiles[i:i+4])+'</tr>' for i in range(0,len(tiles),4)]
        doc=QTextDocument();doc.setHtml('<html><body style="font-family:Arial"><h2>'+html.escape(title)+'</h2><p>LEGO Collection Manager · '+datetime.now().strftime('%d.%m.%Y')+'</p><table width="100%">'+''.join(rows)+'</table></body></html>')
        printer=QPrinter(QPrinter.HighResolution);printer.setPageSize(QPageSize(QPageSize.A4))
        dialog=QPrintDialog(printer,self)
        if dialog.exec()==QDialog.Accepted:doc.print_(printer)
    def closeEvent(self,event):
        self.images.stopping=True
        self.images.pending.clear()
        self.db.db.commit()
        super().closeEvent(event)

class SimpleModel(QAbstractTableModel):
    def __init__(self,headers,rows):super().__init__();self.headers=headers;self.rows=rows
    def set_rows(self,rows):self.beginResetModel();self.rows=rows;self.endResetModel()
    def rowCount(self,p=QModelIndex()):return 0 if p.isValid() else len(self.rows)
    def columnCount(self,p=QModelIndex()):return len(self.headers)
    def data(self,index,role=Qt.DisplayRole):
        if role==Qt.DisplayRole and index.isValid():return str(self.rows[index.row()][index.column()])
        return None
    def headerData(self,section,orientation,role=Qt.DisplayRole):
        return self.headers[section] if orientation==Qt.Horizontal and role==Qt.DisplayRole else None

class LooseModel(SimpleModel):
    def __init__(self,images,rows):
        super().__init__(['Снимка','Част / номер','Цвят','Брой налични'],rows)
        self.images=images;self.parts=[]
    def set_rows(self,rows,parts=None):
        self.parts=parts or []
        super().set_rows(rows)
    def data(self,index,role=Qt.DisplayRole):
        if index.isValid() and index.column()==0 and role==Qt.DecorationRole and index.row()<len(self.parts):
            pm=self.images.get(self.parts[index.row()].get('image',''),(138,102))
            return pm if not pm.isNull() else None
        if role==Qt.TextAlignmentRole and index.isValid() and index.column()==3:return Qt.AlignCenter
        if role==Qt.ToolTipRole and index.isValid() and index.row()<len(self.parts):
            p=self.parts[index.row()];return p['name']+' · '+p['part_num']
        return super().data(index,role)

class MissingModel(SimpleModel):
    def __init__(self, images, rows):
        super().__init__(['Снимка','Част / номер','Цвят','Липсват общо','В комплекти'],rows)
        self.images=images
        self.parts=[]
        self.set_sources={}

    def set_rows(self,rows,parts=None):
        self.parts=parts or []
        super().set_rows(rows)

    def sources_for(self,row):
        if 0 <= row < len(self.parts):
            part=self.parts[row]
            return self.set_sources.get((part['part_num'],part['color_id']),[])
        return []

    def data(self,index,role=Qt.DisplayRole):
        if not index.isValid():return None
        if index.column()==0 and role==Qt.DecorationRole and index.row()<len(self.parts):
            pm=self.images.get(self.parts[index.row()].get('image',''),(148,109))
            return pm if not pm.isNull() else None
        if index.column()==4:
            if role==Qt.DisplayRole or role==Qt.DecorationRole:
                return None
            if role==Qt.ToolTipRole:
                return 'Снимка, номер и липсващи бройки за всеки комплект. Двоен клик за подробности.'
        if role==Qt.TextAlignmentRole and index.column()>=3:
            return Qt.AlignCenter
        if role==Qt.ToolTipRole and index.row()<len(self.parts):
            p=self.parts[index.row()]
            return p['name']+' · '+p['part_num']
        return super().data(index,role)

if __name__=='__main__':
    if sys.platform.startswith('win'):
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass
    app=QApplication(sys.argv);app.setStyle('Fusion');app.setWindowIcon(QIcon(str(PROGRAM_DIR / 'collector_icon.ico')));app.setStyleSheet(CSS)
    window=MainWindow();window.show();sys.exit(app.exec())
