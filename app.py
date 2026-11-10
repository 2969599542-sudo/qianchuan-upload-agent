import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime

from agent.state import atomic_json
from agent.worker import run
from agent.migration import import_legacy

BASE = Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'QianchuanAgent'


def main():
    BASE.mkdir(parents=True,exist_ok=True)
    configs = BASE/'configs'
    configs.mkdir(exist_ok=True)
    app = tk.Tk()
    app.title('千川共享盘上传助手 0.1.0')
    app.geometry('860x650')
    frame = ttk.Frame(app,padding=16)
    frame.pack(fill='both',expand=True)
    ttk.Label(frame,text='确认入库后立即删除原片 · 同账户多电脑互斥 · 无需GPT',font=('Microsoft YaHei UI',12)).pack(anchor='w')
    ttk.Label(frame,text='首次使用请配置账户。旧程序必须先停用，并导入历史记录。',wraplength=800).pack(anchor='w',pady=(5,12))
    fields = {}
    for key,label in [('share_root','共享盘总目录'),('folder','对应账户文件夹名称'),('account_id','千川账户ID'),('account_name','千川账户完整名称')]:
        line = ttk.Frame(frame)
        line.pack(fill='x',pady=3)
        ttk.Label(line,text=label,width=22).pack(side='left')
        fields[key] = tk.StringVar()
        ttk.Entry(line,textvariable=fields[key]).pack(side='left',fill='x',expand=True)
        if key=='share_root':
            def choose():
                chosen = filedialog.askdirectory(title='选择所有账户文件夹所在的共享总目录')
                if chosen:
                    fields['share_root'].set(chosen)
            ttk.Button(line,text='选择',command=choose).pack(side='left')
    protected = tk.BooleanVar(value=True)
    ttk.Checkbutton(frame,text='主户保护：每日全局自动上传699条，超额逐条确认',variable=protected).pack(anchor='w',pady=8)
    ttk.Label(frame,text='此版本所有账户（含主户）均在确认入库后立即删除原片。登录只保存在本机专用Edge配置中。',wraplength=800).pack(anchor='w')
    row = ttk.Frame(frame)
    row.pack(fill='x',pady=8)
    profile = ttk.Combobox(row,state='readonly',values=[p.stem for p in configs.glob('*.json')])
    profile.pack(side='left')
    workers = {}
    events = queue.Queue()
    status = tk.StringVar(value='尚未启动；可先保存配置并导入旧记录')
    ttk.Label(frame,textvariable=status,wraplength=800).pack(anchor='w',pady=5)
    output = tk.Text(frame,wrap='word',height=18,state='disabled')
    output.pack(fill='both',expand=True)

    def report(account,text):
        line = datetime.now().isoformat(timespec='seconds')+' ['+account+'] '+text
        with (BASE/'agent.log').open('a',encoding='utf-8') as out:
            out.write(line+'\n')
        events.put(('log',line))

    def config():
        c = {k:v.get().strip() for k,v in fields.items()}
        c['protected'] = protected.get()
        if not c['account_id'].isdigit() or not c['account_name']:
            raise ValueError('请填写正确的账户ID和完整账户名称')
        if not c['folder'] or Path(c['folder']).name!=c['folder'] or c['folder'] in ('.','..'):
            raise ValueError('文件夹名称只能填写共享总目录下的一级账户文件夹名称')
        if not (Path(c['share_root'])/c['folder']).is_dir():
            raise ValueError('账户文件夹不可访问，请检查共享盘路径和权限')
        return c

    def save():
        try:
            c = config()
            atomic_json(configs/(c['account_id']+'.json'),c)
            profile['values'] = [p.stem for p in configs.glob('*.json')]
            profile.set(c['account_id'])
            status.set('配置已保存；请确认旧程序已停止并已导入旧记录')
            return c
        except Exception as e:
            messagebox.showerror('配置未保存',str(e),parent=app)

    def load(event=None):
        c = json.loads((configs/(profile.get()+'.json')).read_text(encoding='utf-8'))
        for k,v in fields.items():
            v.set(c[k])
        protected.set(c['protected'])
    profile.bind('<<ComboboxSelected>>',load)

    def approve(account,name,count,stop):
        done = threading.Event()
        result = [False]
        events.put(('approve',(account,name,count,done,result)))
        while not done.wait(.5):
            if stop.is_set():
                return False
        return result[0]

    def start():
        c = save()
        if not c:
            return
        aid = c['account_id']
        if aid in workers and workers[aid][1].is_alive():
            status.set('该账户已经运行')
            return
        stop = threading.Event()
        thread = threading.Thread(target=run,args=(c,BASE/aid,stop,lambda text:report(aid,text),lambda name,count:approve(aid,name,count,stop)),daemon=True)
        workers[aid] = (stop,thread)
        thread.start()
        status.set('正在连接共享盘和账户，请在专用Edge窗口手动登录')

    def pause():
        for stop,_ in workers.values():
            stop.set()
        status.set('已请求停止所有账户；在途文件保留待核对，不会删除')

    def migrate():
        try:
            c = config()
            folder = filedialog.askdirectory(title='选择含upload-config.json和upload-state.sqlite的旧账户目录')
            if folder:
                result = import_legacy(folder,c['share_root'],c['account_id'])
                messagebox.showinfo('导入完成',str(result)+'\n未核实记录仅允许核对，不能直接重传。',parent=app)
        except Exception as e:
            messagebox.showerror('导入未完成',str(e),parent=app)

    ttk.Button(row,text='保存配置',command=save).pack(side='left',padx=5)
    ttk.Button(row,text='开始 / 继续',command=start).pack(side='left',padx=5)
    ttk.Button(row,text='停止全部',command=pause).pack(side='left',padx=5)
    ttk.Button(row,text='导入旧记录',command=migrate).pack(side='left',padx=5)

    def tick():
        while not events.empty():
            kind,value = events.get()
            if kind=='log':
                status.set(value)
                output.configure(state='normal')
                output.insert('end',value+'\n')
                output.see('end')
                output.configure(state='disabled')
            else:
                aid,name,count,done,result = value
                result[0] = messagebox.askyesno('主户额外上传确认',f'账户 {aid} 今日已计数 {count} 条。\n是否仅额外批准这一条？\n{name}',default='no',parent=app)
                done.set()
        app.after(500,tick)
    app.protocol('WM_DELETE_WINDOW',app.iconify)
    tick()
    app.mainloop()


if __name__=='__main__':
    main()
