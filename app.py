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
from agent.gpt import Advisor, settings, save_settings

BASE = Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'QianchuanAgent'


def main():
    BASE.mkdir(parents=True,exist_ok=True)
    configs = BASE/'configs'
    configs.mkdir(exist_ok=True)
    app = tk.Tk()
    app.title('千川共享盘上传助手 0.2.0 · GPT辅助')
    app.geometry('860x650')
    frame = ttk.Frame(app,padding=16)
    frame.pack(fill='both',expand=True)
    ttk.Label(frame,text='确认入库后立即删除原片 · 同账户多电脑互斥 · GPT诊断',font=('Microsoft YaHei UI',12)).pack(anchor='w')
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
    advisor = Advisor(BASE, lambda text: events.put(('gpt', text)))
    status = tk.StringVar(value='尚未启动；可先保存配置并导入旧记录')
    ttk.Label(frame,textvariable=status,wraplength=800).pack(anchor='w',pady=5)
    output = tk.Text(frame,wrap='word',height=18,state='disabled')
    output.pack(fill='both',expand=True)

    def report(account,text):
        line = datetime.now().isoformat(timespec='seconds')+' ['+account+'] '+text
        with (BASE/'agent.log').open('a',encoding='utf-8') as out:
            out.write(line+'\n')
        events.put(('log',line))
        advisor.observe(text)

    def gpt_settings():
        dialog = tk.Toplevel(app)
        dialog.title('GPT设置 · 密钥只在本机加密保存')
        dialog.geometry('620x340')
        box = ttk.Frame(dialog,padding=16)
        box.pack(fill='both',expand=True)
        try:
            conf = settings(BASE)
        except (ValueError,OSError):
            conf = {}
        ttk.Label(box,text='OpenAI API密钥（已保存时可留空；不显示旧密钥）').pack(anchor='w')
        key = ttk.Entry(box,show='*',width=65)
        key.pack(fill='x',pady=5)
        ttk.Label(box,text='模型名称（需你的API账户有权限）').pack(anchor='w')
        model = ttk.Entry(box)
        model.insert(0,conf.get('model','gpt-4.1-mini'))
        model.pack(fill='x',pady=5)
        auto = tk.BooleanVar(value=conf.get('automatic',False))
        ttk.Checkbutton(box,text='异常时自动诊断（每台电脑最多每5分钟一次，会产生API费用）',variable=auto).pack(anchor='w',pady=5)
        ttk.Label(box,text='只发送预设的状态分类，不发送视频、文件名、账户ID或原始日志。\nGPT只提供建议，不执行命令、上传或删除。API费用单独计费。',wraplength=570).pack(anchor='w',pady=5)
        def persist(test=False):
            try:
                save_settings(BASE,key.get(),model.get(),auto.get())
                key.delete(0,'end')
                dialog.destroy()
                status.set('GPT设置已保存，密钥由当前Windows用户加密保护')
                if test:
                    advisor.ask()
            except Exception:
                messagebox.showerror('设置未保存','请检查密钥和模型名称；首次使用密钥不能为空。',parent=dialog)
        buttons = ttk.Frame(box)
        buttons.pack(fill='x',pady=8)
        ttk.Button(buttons,text='保存',command=persist).pack(side='left')
        ttk.Button(buttons,text='保存并测试连接',command=lambda:persist(True)).pack(side='left',padx=8)
        def forget():
            (BASE/'gpt-settings.json').unlink(missing_ok=True)
            dialog.destroy()
            status.set('已移除本机GPT密钥；正在进行的诊断可能仍会返回')
        ttk.Button(buttons,text='移除密钥',command=forget).pack(side='left')

    def diagnose():
        if advisor.ask():
            status.set('GPT正在分析状态分类，请稍候；上传流程继续独立运行')
        else:
            status.set('已有GPT诊断正在进行，请稍候')

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
    gpt_row = ttk.Frame(frame)
    gpt_row.pack(fill='x',pady=4)
    ttk.Button(gpt_row,text='GPT设置',command=gpt_settings).pack(side='left')
    ttk.Button(gpt_row,text='请GPT诊断',command=diagnose).pack(side='left',padx=8)

    def tick():
        while not events.empty():
            kind,value = events.get()
            if kind in ('log','gpt'):
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
    if not (BASE/'gpt-settings.json').exists():
        app.after(500,gpt_settings)
    app.mainloop()


if __name__=='__main__':
    main()
