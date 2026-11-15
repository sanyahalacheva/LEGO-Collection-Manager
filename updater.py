"""Independent desktop updater. Bundled as a separate Windows executable."""
from __future__ import annotations
import argparse, json, os, queue, shutil, subprocess, sys, tempfile, threading, time, traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from update_core import (github_latest, read_version, parse_version, download,
    RELEASE_ZIP, HASH_NAME, verify_sha, safe_extract, replace_app)

ROOT=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent


def wait_for_exit(pid,timeout=65):
    if not pid:return
    if sys.platform!='win32':return
    import ctypes
    PROCESS_SYNCHRONIZE=0x00100000
    handle=ctypes.windll.kernel32.OpenProcess(PROCESS_SYNCHRONIZE,False,int(pid))
    if handle:
        try:
            status=ctypes.windll.kernel32.WaitForSingleObject(handle,int(timeout*1000))
            if status != 0:raise TimeoutError('LEGO Manager не се затвори навреме. Затвори го и опитай пак.')
        finally:ctypes.windll.kernel32.CloseHandle(handle)


class Updater:
    def __init__(self,root,pid=0):
        self.target=root;self.pid=pid;self.events=queue.Queue();self.running=False
        self.win=tk.Tk();self.win.title('LEGO Collection Manager — Обновяване');self.win.geometry('630x340');self.win.minsize(565,310)
        self.win.configure(bg='#f7f9fd')
        container=ttk.Frame(self.win,padding=22);container.pack(fill='both',expand=True)
        ttk.Label(container,text='Обновяване на LEGO Collection Manager',font=('Segoe UI',15,'bold')).pack(anchor='w')
        self.current=read_version(root)
        ttk.Label(container,text=f'Инсталирана версия: {self.current}    ·    Данните в data не се заменят.',wraplength=560).pack(anchor='w',pady=(10,13))
        row=ttk.Frame(container);row.pack(fill='x')
        self.online=ttk.Button(row,text='Провери GitHub',command=self.check);self.online.pack(side='left',padx=(0,12))
        self.offline=ttk.Button(row,text='Обнови от ZIP файл…',command=self.offline_pick);self.offline.pack(side='left')
        self.status=tk.StringVar(value='Можеш да провериш онлайн или да избереш официален ZIP от флашка.')
        ttk.Label(container,textvariable=self.status,wraplength=560).pack(anchor='w',pady=(19,10))
        self.progress=ttk.Progressbar(container,mode='indeterminate');self.progress.pack(fill='x',pady=(0,12))
        self.install_btn=ttk.Button(container,text='Инсталирай намерената версия',command=self.install_online)
        self.install_btn.pack(anchor='w');self.install_btn.state(['disabled'])
        self.available=None
        ttk.Label(container,text='При неуспешно обновяване старите програмни файлове се възстановяват.\nПреди първото обновяване запази копие на цялата папка.',wraplength=560).pack(anchor='w',pady=(14,0))
        self.win.after(120,self.poll)
    def busy(self,value):
        self.running=value
        for button in [self.online,self.offline]:button.state(['disabled'] if value else ['!disabled'])
        if value:self.progress.start(10)
        else:self.progress.stop()
    def worker(self,func):
        if self.running:return
        self.busy(True)
        def run():
            try:self.events.put(('done',func()))
            except Exception as e:self.events.put(('error',str(e)))
        threading.Thread(target=run,daemon=True).start()
    def poll(self):
        try:
            while True:
                what,value=self.events.get_nowait()
                if what=='status':self.status.set(value)
                elif what=='done':
                    self.busy(False)
                    if value=='installed':
                        messagebox.showinfo('Готово','Обновяването завърши успешно. Стартирам LEGO Manager.')
                        exe=self.target/'LEGO Collection Manager.exe'
                        if exe.exists():subprocess.Popen([str(exe)],cwd=str(self.target))
                        self.win.destroy();return
                    if isinstance(value,tuple):
                        self.available=value
                        self.status.set('Налична версия '+value[0])
                        if parse_version(value[0])>parse_version(self.current):
                            self.install_btn.state(['!disabled'])
                            messagebox.showinfo('Нова версия',f'Има версия {value[0]}. Натисни „Инсталирай“.')
                        else:
                            self.install_btn.state(['disabled'])
                            self.status.set('Вече използваш последната версия ('+self.current+').')
                elif what=='error':
                    self.busy(False);self.status.set('Грешка: '+value);messagebox.showerror('Неуспешно обновяване',value)
        except queue.Empty:pass
        self.win.after(120,self.poll)
    def check(self):
        self.status.set('Проверявам GitHub…')
        self.worker(lambda:github_latest(self.target))
    def offline_pick(self):
        path=filedialog.askopenfilename(title='Избери официален update ZIP',filetypes=[('ZIP архив','*.zip')])
        if not path:return
        sha=Path(path+'.sha256')
        if not sha.is_file():
            messagebox.showwarning('Липсва проверка',f'Постави до ZIP файла и неговия .sha256 файл:\n{sha.name}')
            return
        if not messagebox.askyesno('Потвърждение','Обновяване от локален ZIP? Увери се, че е свален от официалното GitHub Releases хранилище.'):
            return
        self.status.set('Проверявам и инсталирам офлайн обновлението…')
        self.worker(lambda:self.apply(Path(path),sha))
    def install_online(self):
        if not self.available:return
        version,urls,_=self.available
        if not messagebox.askyesno('Обновяване',f'Инсталиране на версия {version}?\nLEGO Manager ще бъде затворен. Данните се запазват.'):
            return
        self.status.set('Изтеглям и проверявам файловете…')
        def run():
            with tempfile.TemporaryDirectory(prefix='lego_download_') as temp:
                zip_path=Path(temp)/RELEASE_ZIP;sha=Path(temp)/HASH_NAME
                download(urls[RELEASE_ZIP],zip_path)
                download(urls[HASH_NAME],sha,max_size=4096)
                return self.apply(zip_path,sha)
        self.worker(run)
    def apply(self,zip_path,sha):
        verify_sha(zip_path,sha)
        with tempfile.TemporaryDirectory(prefix='.lego_stage_',dir=self.target) as tmp:
            stage=Path(tmp)
            safe_extract(zip_path,stage)
            new_version=json.loads((stage/'version.json').read_text(encoding='utf8'))['version']
            if parse_version(new_version)<=parse_version(self.current):
                raise ValueError('Избраната версия не е по-нова от инсталираната.')
            self.events.put(('status','Изчаквам приложението да се затвори…'))
            wait_for_exit(self.pid)
            self.events.put(('status','Създавам резервно копие и обновявам…'))
            replace_app(self.target,stage,lambda msg:self.events.put(('status',msg)))
        return 'installed'
    def mainloop(self):self.win.mainloop()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--pid',type=int,default=0)
    args=parser.parse_args()
    Updater(ROOT,args.pid).mainloop()
