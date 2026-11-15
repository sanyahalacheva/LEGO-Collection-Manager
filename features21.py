"""Optional screens and layout for LEGO Collection Manager 2.1.
No user data is written during import. All SQLite access stays on the UI thread.
"""
from __future__ import annotations
import csv, html, json, re
from pathlib import Path
from collections import defaultdict
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QWidget,QDialog,QVBoxLayout,QHBoxLayout,QGridLayout,
 QLineEdit,QScrollArea,QLabel,QPushButton,QComboBox,QSpinBox,QMessageBox,QCheckBox,
 QFileDialog,QFrame,QSizePolicy,QSlider,QProgressBar)

# Kept separate so an image-recognition service can later be plugged in without
# changing inventory accounting or silently uploading user photos.
class PhotoRecognitionProvider:
    def identify(self, image_path):
        raise NotImplementedError('Разпознаването по снимка не е включено.')


def _lbl(s, bold=False):
    w=QLabel(str(s));w.setWordWrap(True)
    if bold:w.setStyleSheet('font-weight:700;font-size:15px')
    return w


def _btn(text,fn=None):
    b=QPushButton(text);b.setMinimumHeight(34)
    if fn:b.clicked.connect(fn)
    return b


def _clear(layout):
    while layout.count():
        child=layout.takeAt(0)
        if child.widget():child.widget().deleteLater()
        elif child.layout():_clear(child.layout())


def _image(owner,url,size=96):
    l=QLabel('·');l.setAlignment(Qt.AlignCenter);l.setFixedSize(size,size)
    l.setProperty('image_url',url)
    if url:
        p=owner.images.get(url,(size-6,size-6))
        if not p.isNull():l.setPixmap(p)
    return l


class KidCheckDialog(QDialog):
    def __init__(self,owner,set_row):
        super().__init__(owner)
        self.owner=owner;self.set_row=set_row
        self.setWindowTitle('Подреждаме LEGO · '+set_row['set_num']);self.resize(850,690)
        self.parts=[p for p in owner.db.parts_for(set_row['id']) if not p['spare'] and not p['alternate']]
        self.index=0
        self.layout_main=QVBoxLayout(self)
        self.info=_lbl('');self.layout_main.addWidget(self.info)
        self.picture=_image(owner,'',290);self.layout_main.addWidget(self.picture,0,Qt.AlignCenter)
        self.partlabel=_lbl('',True);self.partlabel.setAlignment(Qt.AlignCenter);self.layout_main.addWidget(self.partlabel)
        self.amount=_lbl('');self.amount.setAlignment(Qt.AlignCenter);self.amount.setStyleSheet('font-size:21px');self.layout_main.addWidget(self.amount)
        controls=QHBoxLayout();self.minus=_btn('−',lambda:self.change(-1));self.plus=_btn('+',lambda:self.change(1))
        self.all_btn=_btn('✓ Всички налични',self.fill)
        for b in (self.minus,self.plus,self.all_btn):b.setMinimumHeight(60);controls.addWidget(b)
        self.layout_main.addLayout(controls)
        nav=QHBoxLayout();nav.addWidget(_btn('← Предишна',lambda:self.navigate(-1)));nav.addWidget(_btn('Следваща →',lambda:self.navigate(1)));self.layout_main.addLayout(nav)
        self.layout_main.addWidget(_btn('Затвори',self.accept))
        self.owner.images.updated.connect(self.image_update)
        self.finished.connect(lambda *_:self.disconnect_image())
        self.restore_index();self.draw()
    def disconnect_image(self):
        try:self.owner.images.updated.disconnect(self.image_update)
        except (TypeError,RuntimeError):pass
    def image_update(self,url):
        if self.parts and self.parts[self.index].get('image')==url:
            pix=self.owner.images.get(url,(284,284))
            if not pix.isNull():self.picture.setPixmap(pix)
    def restore_index(self):
        data=self.owner.db.row('SELECT idx FROM kid_progress WHERE set_id=?',(self.set_row['id'],))
        if data:self.index=max(0,min(len(self.parts)-1,int(data['idx']))) if self.parts else 0
    def store_index(self):
        with self.owner.db.db:self.owner.db.db.execute('INSERT INTO kid_progress(set_id,idx) VALUES(?,?) ON CONFLICT(set_id) DO UPDATE SET idx=excluded.idx',(self.set_row['id'],self.index))
    def draw(self):
        if not self.parts:
            self.info.setText('Няма части за проверка.');return
        p=self.parts[self.index]
        self.info.setText(f'Част {self.index+1} от {len(self.parts)} · {self.set_row["set_num"]}')
        self.partlabel.setText(f'{p["name"][:85]} · {p["color"]} · #{p["part_num"]}')
        self.picture.setPixmap(QPixmap());self.picture.setText('·')
        url=p.get('image') or '';self.picture.setProperty('image_url',url)
        if url:
            pix=self.owner.images.get(url,(284,284))
            if not pix.isNull():self.picture.setText('');self.picture.setPixmap(pix)
        self.amount.setText(f'Налични {p["owned"]} / {p["required"]}    ·    Липсват {max(0,p["required"]-p["owned"])}')
        self.store_index()
    def change(self,step):
        if not self.parts:return
        p=self.parts[self.index]
        self.owner.db.set_owned(p['id'],p['owned']+step)
        p['owned']=self.owner.db.row('SELECT owned FROM parts WHERE id=?',(p['id'],))['owned']
        self.draw();self.owner.on_data_changed(False)
    def fill(self):
        if not self.parts:return
        p=self.parts[self.index]
        self.owner.db.set_owned(p['id'],p['required']);p['owned']=p['required'];self.draw();self.owner.on_data_changed(False)
    def navigate(self,delta):
        if self.parts:self.index=max(0,min(len(self.parts)-1,self.index+delta));self.draw()


class Features21:
    def feature_init(self):
        self.theme_mode='По серии';self.theme_collapsed=set()
        self.photo_recognition_provider=None
        self.theme_status=_lbl('')
        self._feature_migrate()
    def _feature_migrate(self):
        self.db.db.execute('PRAGMA busy_timeout=5000')
        columns={x['name'] for x in self.db.rows('PRAGMA table_info(sets)')}
        if 'theme_id' not in columns:
            with self.db.db:self.db.db.execute('ALTER TABLE sets ADD COLUMN theme_id INTEGER')
        if 'theme_name' not in columns:
            with self.db.db:self.db.db.execute("ALTER TABLE sets ADD COLUMN theme_name TEXT NOT NULL DEFAULT ''")
        with self.db.db:
            self.db.db.execute('CREATE TABLE IF NOT EXISTS themes (id INTEGER PRIMARY KEY,name TEXT NOT NULL,parent_id INTEGER)')
            self.db.db.execute('CREATE TABLE IF NOT EXISTS kid_progress (set_id INTEGER PRIMARY KEY REFERENCES sets(id) ON DELETE CASCADE,idx INTEGER NOT NULL DEFAULT 0)')
            self.db.db.execute('CREATE TABLE IF NOT EXISTS set_box_art (set_num TEXT PRIMARY KEY, image_path TEXT NOT NULL)')
            self.db.db.execute('PRAGMA user_version=3')
    def feature_register_theme(self,sid,entry):
        tid=entry.get('theme_id')
        if tid is not None:
            with self.db.db:self.db.db.execute('UPDATE sets SET theme_id=? WHERE id=?',(int(tid),sid))
    def feature_make_pages(self):
        self._feature_migrate()
        self.make_reverse_page();self.make_finish_page()
    def feature_set_header(self,h):
        self.theme_mode_combo=QComboBox();self.theme_mode_combo.addItems(['По серии','Всички комплекти'])
        self.theme_mode_combo.currentTextChanged.connect(self.feature_mode_change)
        h.addWidget(self.theme_mode_combo)
        h.addWidget(_btn('↻ Разпознай сериите',self.feature_sync_themes))
        h.addWidget(_btn('Задай серия',self.feature_edit_theme))
    def feature_mode_change(self,text):
        self.theme_mode=text;self.show_sets()
    def _theme_logo_dir(self):
        base=self.db_path_folder()/'theme_logos'
        base.mkdir(parents=True,exist_ok=True)
        return base
    def db_path_folder(self):
        from pathlib import Path
        return Path(self.db.db.execute('PRAGMA database_list').fetchone()[2]).parent
    def feature_logo_path(self,name):
        # Only actual, separately provided logo assets are displayed here;
        # we never present generated text as an official trademark logo.
        base=self._theme_logo_dir()
        key=re.sub('[^a-z0-9]+','_',name.casefold()).strip('_')
        for extension in ('.png','.jpg','.webp'):
            path=base/(key+extension)
            if path.is_file():return path
        return None
    def feature_top_theme(self,tid):
        if tid is None:return 'Без определена серия'
        themes={x['id']:x for x in self.db.rows('SELECT id,name,parent_id FROM themes')}
        current=tid;seen=set();ancestors=[]
        while current in themes and current not in seen:
            seen.add(current);item=themes[current];ancestors.append(item['name'])
            if not item['parent_id']:break
            current=item['parent_id']
        if not ancestors:return 'Неразпозната серия'
        # Rebrickable sometimes puts City under Town, Marvel under Super Heroes,
        # etc. Choose a recognizable top-level product line, not an abstract
        # database root such as "Modern Day" or "Licensed".
        desired=('city','technic','marvel','star wars','friends','ninjago',
                 'creator','icons','disney','minecraft','harry potter','dc',
                 'speed champions','jurassic world','architecture','ideas',
                 'duplo','monkie kid','dreamzzz','super mario','sonic',
                 'botanical','art','classic','hidden side','vidiyo','space')
        normalized=[(name,name.casefold()) for name in ancestors]
        for target in desired:
            for name,norm in normalized:
                if norm==target or norm.startswith(target+' '):return name
        # If the tree has a meaningful child under a broad root, group by it.
        generic={'modern day','licensed','historical','other','town','super heroes'}
        for name,norm in reversed(normalized[:-1]):
            if norm not in generic:return name
        return ancestors[-1]
    def feature_box_dir(self):
        folder=self.db_path_folder()/'box_art'
        folder.mkdir(parents=True,exist_ok=True)
        return folder
    def feature_box_path(self,set_number):
        record=self.db.row('SELECT image_path FROM set_box_art WHERE set_num=?',(set_number,))
        if not record:return None
        filename=record['image_path']
        # Never trust arbitrary paths stored in the database.
        path=self.feature_box_dir()/Path(filename).name
        return path if path.exists() and path.is_file() else None
    def feature_cleanup_box_art(self,set_number):
        # A box photo is shared by duplicate copies of the same LEGO set.
        if self.db.count('SELECT COUNT(*) FROM sets WHERE set_num=?',(set_number,)):
            return
        path=self.feature_box_path(set_number)
        with self.db.db:self.db.db.execute('DELETE FROM set_box_art WHERE set_num=?',(set_number,))
        if path:
            try:path.unlink(missing_ok=True)
            except OSError:pass
    def feature_set_box_art(self,set_number):
        file_name,_=QFileDialog.getOpenFileName(self,'Снимка на оригиналната кутия за '+set_number,'','Изображения (*.png *.jpg *.jpeg *.webp)')
        if not file_name:return
        from PySide6.QtGui import QImage
        image=QImage(file_name)
        if image.isNull():
            QMessageBox.warning(self,'Невалидна снимка','Избраният файл не може да се отвори.')
            return
        if max(image.width(),image.height())>1100:
            image=image.scaled(1100,1100,Qt.KeepAspectRatio,Qt.SmoothTransformation)
        # Names are derived only from the LEGO set number, never from a user path.
        safe=re.sub(r'[^a-zA-Z0-9_-]+','_',str(set_number))
        new_name='box_'+safe+'.png'
        destination=self.feature_box_dir()/new_name
        staged=destination.with_suffix('.tmp.png')
        if not image.save(str(staged),'PNG'):
            QMessageBox.warning(self,'Грешка','Снимката не можа да бъде запазена.')
            return
        staged.replace(destination)
        with self.db.db:self.db.db.execute('INSERT INTO set_box_art(set_num,image_path) VALUES(?,?) ON CONFLICT(set_num) DO UPDATE SET image_path=excluded.image_path',(set_number,new_name))
        self.show_sets()
    def feature_choose_group_logo(self,name):
        file_name,_=QFileDialog.getOpenFileName(self,'Избери истинско лого за '+name,'','Лога (*.png *.jpg *.jpeg *.webp)')
        if not file_name:return
        from PySide6.QtGui import QImage
        image=QImage(file_name)
        if image.isNull():
            QMessageBox.warning(self,'Грешка','Неуспешно разчитане на логото.');return
        if max(image.width(),image.height())>1400:
            image=image.scaled(1400,1400,Qt.KeepAspectRatio,Qt.SmoothTransformation)
        key=re.sub('[^a-z0-9]+','_',name.casefold()).strip('_')
        if not key:return
        destination=self._theme_logo_dir()/(key+'.png')
        staged=destination.with_suffix('.tmp.png')
        if not image.save(str(staged),'PNG'):
            QMessageBox.warning(self,'Грешка','Логото не можа да се запази.');return
        staged.replace(destination)
        self.show_sets()
    def feature_collection_card(self,s,duplicates):
        card=QFrame();card.setObjectName('legoCard')
        card.setMinimumWidth(238)
        v=QVBoxLayout(card);v.setContentsMargins(15,15,15,15);v.setSpacing(10)
        stage=QFrame();stage.setObjectName('imageStage');stage.setMinimumHeight(208)
        stage_layout=QVBoxLayout(stage);stage_layout.setContentsMargins(8,8,8,8)
        box=self.feature_box_path(s['set_num'])
        if box is not None:
            image=QLabel();image.setAlignment(Qt.AlignCenter);image.setFixedHeight(185)
            pm=QPixmap(str(box)).scaled(225,177,Qt.KeepAspectRatio,Qt.SmoothTransformation)
            image.setPixmap(pm);stage_layout.addWidget(image,0,Qt.AlignCenter)
        else:
            preview=_image(self,s['image'],185);stage_layout.addWidget(preview,0,Qt.AlignHCenter)
        v.addWidget(stage)
        title=_lbl(html.escape(s['name']),True)
        title.setObjectName('cardTitle')
        title.setMinimumHeight(38);v.addWidget(title)
        details=_lbl(f"{html.escape(s['set_num'])} · Екземпляр #{s['id']}"+(f' · {duplicates} еднакви' if duplicates>1 else ''))
        details.setObjectName('statusCount');v.addWidget(details)
        if not s['started']:
            status=_lbl('○  Не е започната проверка');status.setObjectName('statusNew')
            progress=0
            info='Проверката още не е започнала'
        elif s['total']==0 or s['owned']>=s['total']:
            status=_lbl('✓  Напълно комплектован');status.setObjectName('statusDone')
            progress=100
            info=f"Налични: {s['owned']} / {s['total']}"
        else:
            status=_lbl('◔  Проверява се');status.setObjectName('statusWork')
            progress=int(100*s['owned']/max(1,s['total']))
            info=f"Налични: {s['owned']} / {s['total']}   ·   Липсват: {max(0,s['total']-s['owned'])}"
        v.addWidget(status)
        meter=QProgressBar();meter.setRange(0,100);meter.setValue(progress);meter.setTextVisible(False)
        meter.setObjectName('progressNew' if not s['started'] else ('progressDone' if progress==100 else 'progressWork'))
        meter.setToolTip(f'{progress}% налични');v.addWidget(meter)
        hint=_lbl(info);hint.setObjectName('statusCount');v.addWidget(hint)
        buttons=QHBoxLayout();buttons.setSpacing(8)
        open_button=_btn('Отвори',lambda checked=False,id=s['id']:self.open_set(id))
        open_button.setObjectName('primary')
        buttons.addWidget(open_button,2)
        remove=_btn('Изтрий',lambda checked=False,id=s['id']:self.confirm_delete_set(id));remove.setObjectName('danger')
        buttons.addWidget(remove,1)
        v.addLayout(buttons)
        return card
    def feature_show_sets(self):
        _clear(self.sets_grid)
        rows=self.db.all_sets();counts=defaultdict(int)
        for s in rows:counts[s['set_num']]+=1
        if not rows:
            self.sets_grid.addWidget(_lbl('Колекцията е празна. Добави първия комплект през „Търсене“.'));return
        categories=defaultdict(list)
        if self.theme_mode=='Всички комплекти':categories['Всички комплекти']=rows
        else:
            for s in rows:
                group=(s.get('theme_name') or '').strip() or self.feature_top_theme(s.get('theme_id'))
                categories[group].append(s)
        row_index=0
        for name in sorted(categories,key=str.casefold):
            group=QFrame();group.setObjectName('seriesHeader')
            col=QVBoxLayout(group);col.setContentsMargins(16,14,16,16);col.setSpacing(12)
            head=QHBoxLayout();head.setSpacing(13)
            path=self.feature_logo_path(name) if self.theme_mode!='Всички комплекти' else None
            if path:
                logo=QLabel();logo.setFixedSize(245,78);logo.setAlignment(Qt.AlignLeft|Qt.AlignVCenter)
                pix=QPixmap(str(path));logo.setPixmap(pix.scaled(236,72,Qt.KeepAspectRatio,Qt.SmoothTransformation))
                head.addWidget(logo)
            else:
                title=_lbl(name,True);title.setObjectName('seriesTitle')
                head.addWidget(title)
            count=_lbl(f'{len(categories[name])} комплекта');count.setObjectName('statusCount');head.addWidget(count)
            head.addStretch()
            head.addWidget(_btn('Разгъни' if name in self.theme_collapsed else 'Сгъни',lambda checked=False,n=name:self.feature_toggle_group(n)))
            col.addLayout(head)
            if name not in self.theme_collapsed:
                cards=QGridLayout();cards.setSpacing(15)
                available=max(300,self.sets_scroll.viewport().width()-75)
                columns=max(1,min(5,available//260))
                for i,s in enumerate(categories[name]):
                    cards.addWidget(self.feature_collection_card(s,counts[s['set_num']]),i//columns,i%columns)
                for i in range(columns):cards.setColumnStretch(i,1)
                col.addLayout(cards)
            self.sets_grid.addWidget(group,row_index,0,1,4);row_index+=1
        for i in range(4):self.sets_grid.setColumnStretch(i,1)
    def feature_toggle_group(self,key):
        if key in self.theme_collapsed:self.theme_collapsed.remove(key)
        else:self.theme_collapsed.add(key)
        self.show_sets()
    def feature_edit_theme(self):
        sets=self.db.all_sets()
        if not sets:
            QMessageBox.information(self,'Серия','Добави поне един комплект.');return
        from PySide6.QtWidgets import QInputDialog
        options=[f"{s['set_num']} · Екз. #{s['id']} · {s['name'][:55]}" for s in sets]
        choice,ok=QInputDialog.getItem(self,'Серия на комплект','Избери конкретен екземпляр:',options,0,False)
        if not ok:return
        item=sets[options.index(choice)]
        current=item.get('theme_name') or self.feature_top_theme(item.get('theme_id'))
        name,ok=QInputDialog.getText(self,'Име на серия','Серия (напр. City, Technic, Marvel):',text='' if current.startswith(('Неразпозната','Без определена')) else current)
        if not ok or not name.strip():return
        name=name.strip()[:100]
        with self.db.db:self.db.db.execute('UPDATE sets SET theme_name=? WHERE id=?',(name,item['id']))
        self.show_sets()
    def feature_sync_themes(self):
        if not self.api.key:
            QMessageBox.information(self,'API ключ','Необходим е ключ и интернет за разпознаване на сериите.');return
        old=self.db.rows('SELECT id,set_num,theme_id FROM sets WHERE theme_id IS NULL')
        self.statusBar().showMessage(f'Зареждам сериите и проверявам {len(old)} стари комплекта…',10000)
        def work():
            all_themes=self.api.paged('themes/')
            found={}
            by_number={}
            for s in old:
                number=str(s['set_num'])
                if number not in by_number:
                    entry=self.api.get('sets/'+number+'/')
                    by_number[number]=entry.get('theme_id')
                found[s['id']]=by_number[number]
            return all_themes,found
        def done(result):
            themes,found=result
            with self.db.db:
                for t in themes:
                    self.db.db.execute('INSERT INTO themes(id,name,parent_id) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,parent_id=excluded.parent_id',(int(t['id']),t['name'],t.get('parent_id')))
                for sid,tid in found.items():
                    if tid is not None:self.db.db.execute('UPDATE sets SET theme_id=? WHERE id=?',(int(tid),sid))
            self.show_sets();self.statusBar().showMessage('✓ Сериите са обновени.',8000)
        self.run_task(work,done)
    def feature_open_kid(self,set_row):
        if not set_row['started']:
            QMessageBox.information(self,'Започни проверка','Първо отвори комплекта и натисни „Започни проверка“.');return
        KidCheckDialog(self,set_row).exec()
        self.show_sets()
    def make_reverse_page(self):
        page=QWidget();v=QVBoxLayout(page)
        v.addWidget(_lbl('Къде се използва тази част?',True))
        v.addWidget(_lbl('Търси по номер, описание, форма и цвят сред всички свои комплекти и свободни части.'))
        filters=QHBoxLayout()
        self.rev_num=QLineEdit();self.rev_num.setPlaceholderText('Номер')
        self.rev_desc=QLineEdit();self.rev_desc.setPlaceholderText('Описание / размер (2x4)')
        self.rev_color=QLineEdit();self.rev_color.setPlaceholderText('Цвят')
        for f in (self.rev_num,self.rev_desc,self.rev_color):filters.addWidget(f)
        filters.addWidget(_btn('Търси',self.feature_reverse_search));v.addLayout(filters)
        self.rev_scroll=QScrollArea();self.rev_scroll.setWidgetResizable(True)
        holder=QWidget();self.rev_host=holder;self.rev_grid=QVBoxLayout(holder);self.rev_scroll.setWidget(holder);v.addWidget(self.rev_scroll)
        self.stack.addWidget(page)
    def feature_reverse_search(self):
        _clear(self.rev_grid)
        values=(self.rev_num.text().strip(),self.rev_desc.text().strip(),self.rev_color.text().strip())
        if not any(values):self.rev_grid.addWidget(_lbl('Въведи поне едно условие.'));return
        rows=self.db.search_known_parts(number=values[0],description=values[1],shape=values[1],color=values[2],limit=60)
        # Shape and description may be the same search phrase; fallback to plain description.
        if not rows:rows=self.db.search_known_parts(number=values[0],description=values[1],color=values[2],limit=60)
        for p in rows:
            card=QFrame();card.setObjectName('panel');h=QHBoxLayout(card)
            h.addWidget(_image(self,p.get('image') or '',100))
            details=QVBoxLayout();details.addWidget(_lbl(f'<b>{html.escape(p["name"][:75])}</b> · #{p["part_num"]} · {p["color"]}'))
            used=self.db.where_used(p['part_num'],p['color_id'])
            if not used:details.addWidget(_lbl('Няма използване в добавените комплекти.'))
            for usage in used[:30]:
                state=f'{usage["owned"]}/{usage["required"]} налични'
                if usage['started']:state+=f' · липсват {max(0,usage["required"]-usage["owned"])}'
                row=QHBoxLayout();row.addWidget(_lbl(f'{usage["set_num"]} · {usage["set_name"][:65]} · {state}'),1)
                row.addWidget(_btn('Отвори',lambda checked=False,id=usage['set_id']:self.open_set(id)))
                details.addLayout(row)
            loose=self.db.row('SELECT quantity FROM loose WHERE part_num=? AND color_id=?',(p['part_num'],p['color_id']))
            if loose:details.addWidget(_lbl(f'Свободен запас: {loose["quantity"]} бр.'))
            h.addLayout(details,1);self.rev_grid.addWidget(card)
        if not rows:self.rev_grid.addWidget(_lbl('Няма съвпадения в локалната колекция.'))
        self.rev_grid.addStretch()
    def make_finish_page(self):
        page=QWidget();v=QVBoxLayout(page)
        v.addWidget(_lbl('Кои комплекти мога да завърша?',True))
        v.addWidget(_lbl('Показва само предложения. Не премества нищо автоматично.'))
        v.addWidget(_btn('↻ Пресметни отново',self.feature_finish_suggestions))
        sc=QScrollArea();sc.setWidgetResizable(True)
        holder=QWidget();self.finish_host=holder;self.finish_list=QVBoxLayout(holder);sc.setWidget(holder);v.addWidget(sc)
        self.stack.addWidget(page)
    def feature_finish_suggestions(self):
        _clear(self.finish_list)
        stock={(r['part_num'],r['color_id']):r['quantity'] for r in self.db.loose_rows()}
        rows=self.db.rows('''SELECT s.id,s.set_num,s.name,s.image,p.part_num,p.color_id,p.required,p.owned
             FROM sets s JOIN parts p ON p.set_id=s.id
             WHERE s.started=1 AND p.spare=0 AND p.alternate=0 AND p.required>p.owned
             ORDER BY s.id''')
        sets=defaultdict(list)
        for r in rows:sets[r['id']].append(r)
        results=[]
        for sid,parts in sets.items():
            missing=sum(p['required']-p['owned'] for p in parts)
            covered=sum(min(p['required']-p['owned'],stock.get((p['part_num'],p['color_id']),0)) for p in parts)
            results.append((missing-covered,missing,covered,parts[0]))
        results.sort(key=lambda x:(x[0],x[1]))
        if not results:self.finish_list.addWidget(_lbl('Няма започнати комплекти с липсващи части.'))
        for gap,total,covered,info in results:
            box=QFrame();box.setObjectName('panel');h=QHBoxLayout(box)
            h.addWidget(_image(self,info['image'],96))
            col=QVBoxLayout();col.addWidget(_lbl(f'<b>{html.escape(info["name"][:70])}</b> · {info["set_num"]}'))
            col.addWidget(_lbl(f'Липсват {total} бр. · В свободния запас има до {covered} подходящи · Остават {gap} бр.'))
            if gap==0:col.addWidget(_lbl('✓ Може да се комплектова от свободния запас (ако тези части не бъдат използвани за друг комплект).'))
            for part in sets[info['id']]:
                available=stock.get((part['part_num'],part['color_id']),0)
                if available>0:
                    suitable=min(available,part['required']-part['owned'])
                    rr=QHBoxLayout()
                    rr.addWidget(_lbl(f'↳ #{part["part_num"]} · свободни {available} · може да преместиш {suitable} бр.'),1)
                    rr.addWidget(_btn('Избери преместване',lambda checked=False,part=part:self.feature_transfer_part(part)))
                    col.addLayout(rr)
            h.addLayout(col,1);h.addWidget(_btn('Отвори',lambda checked=False,sid=info['id']:self.open_set(sid)))
            self.finish_list.addWidget(box)
        self.finish_list.addStretch()
    def feature_transfer_part(self,part):
        # TransferDialog enforces exact part+color, nonnegative inventory,
        # and a user-chosen source/target. Nothing moves until they confirm.
        row=self.db.row('SELECT id FROM parts WHERE set_id=? AND part_num=? AND color_id=? AND spare=0 AND alternate=0 LIMIT 1',
                        (part['id'],part['part_num'],part['color_id']))
        if not row:return
        # The existing transfer entrypoint presents its own confirmation dialog.
        full=self.db.row('SELECT * FROM parts WHERE id=?',(row['id'],))
        if full:self.open_transfer(full)
        self.feature_finish_suggestions()
    def feature_edit_loose(self,p):
        dialog=QDialog(self);dialog.setWindowTitle('Редакция на свободните части');dialog.resize(520,285)
        v=QVBoxLayout(dialog);h=QHBoxLayout();h.addWidget(_image(self,p.get('image') or '',92))
        h.addWidget(_lbl(f'<b>{html.escape(p["name"][:75])}</b>\n#{p["part_num"]} · {p["color"]}'),1);v.addLayout(h)
        s=QSpinBox();s.setRange(0,1_000_000);s.setValue(p['quantity']);s.setMinimumHeight(44)
        row=QHBoxLayout();row.addWidget(_btn('−',lambda:s.setValue(s.value()-1)));row.addWidget(s,1);row.addWidget(_btn('+',lambda:s.setValue(s.value()+1)));v.addLayout(row)
        v.addWidget(_lbl('Въведи общия брой свободни части. При 0 редът се скрива.'))
        actions=QHBoxLayout();actions.addWidget(_btn('Отказ',dialog.reject))
        def save():
            self.db.set_loose(p,s.value());self.show_loose();self.on_data_changed(False);dialog.accept()
        actions.addWidget(_btn('Запази',save));v.addLayout(actions);dialog.exec()
    def feature_loose_buttons(self,row):
        row.addWidget(_btn('✎ Редактирай броя',self.feature_edit_selected_loose))
    def feature_edit_selected_loose(self):
        selected=self.loose_table.selectionModel().selectedRows()
        if not selected:
            QMessageBox.information(self,'Редакция','Първо маркирай ред от свободните части.');return
        p=self.loose_data[selected[0].row()]
        self.feature_edit_loose(p)
    def feature_export_missing_csv(self):
        path,_=QFileDialog.getSaveFileName(self,'Експорт на липсващите части','missing_parts.csv','CSV файлове (*.csv)')
        if not path:return
        rows=self.db.missing(False)
        # Rebrickable IDs and BrickLink IDs are not guaranteed identical.
        with open(path,'w',newline='',encoding='utf-8-sig') as f:
            out=csv.writer(f);out.writerow(['Rebrickable part number','Rebrickable color ID','Color','Name','Missing quantity','Used in sets'])
            for p in rows:out.writerow([p['part_num'],p['color_id'],p['color'],p['name'],p['missing'],p['num_sets']])
        QMessageBox.information(self,'Готово','Експортът е създаден. Това е CSV за преглед и подготовка на поръчка; НЕ е директен BrickLink Wanted List импорт. Номерата и цветовете може да изискват съпоставяне с каталога на BrickLink.')
