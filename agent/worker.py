from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
import subprocess

from .state import AccountLock, AccountBusy, Ledger, atomic_json, delete_verified
from .browser import Browser

TZ = timezone(timedelta(hours=8))


def validate(file):
    path = str(file).replace("'", "''")
    code = "$s=New-Object -ComObject Shell.Application;$f=Get-Item -LiteralPath '"+path+"';$i=$s.NameSpace($f.DirectoryName).ParseName($f.Name);@($i.ExtendedProperty('System.Video.FrameWidth'),$i.ExtendedProperty('System.Video.FrameHeight'),$i.ExtendedProperty('System.Video.EncodingBitrate'),$i.ExtendedProperty('System.Media.Duration'))|ConvertTo-Json -Compress"
    r = subprocess.run(['powershell.exe','-NoProfile','-Command',code],capture_output=True,encoding='utf-8',timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode or not r.stdout.strip():
        raise RuntimeError('视频参数读取失败，稍后重试')
    try:
        w,h,bitrate,duration = json.loads(r.stdout)
    except (ValueError,TypeError) as error:
        raise RuntimeError('视频参数读取失败') from error
    if not all(isinstance(v,(int,float)) for v in (w,h,bitrate,duration)):
        raise RuntimeError('视频参数未读全')
    ratio = (w*16==h*9 and 720<=w<=1440 and 1280<=h<=2560) or (w*9==h*16 and 1280<=w<=2560 and 720<=h<=1440)
    if not (ratio and bitrate>=516000 and 4<=duration/10000000<=300 and file.stat().st_size<=1000000000):
        raise ValueError(f'参数不符合要求：{w}×{h}，{duration/10000000:.2f}秒，{bitrate/1000:.0f}kbps')


def files_under(root):
    folders = [Path(root)]
    while folders:
        with os.scandir(folders.pop()) as entries:
            for e in entries:
                if e.is_file(follow_symlinks=False) and Path(e.name).suffix.lower() in ('.mp4','.mov','.m4v'):
                    yield Path(e.path), e.stat(follow_symlinks=False)
                elif e.is_dir(follow_symlinks=False) and not Path(e.path).is_junction():
                    folders.append(Path(e.path))


def snapshot(source, root, cache):
    if source.is_symlink() or not source.resolve().is_relative_to(root.resolve()):
        raise ValueError('源文件不在指定账户目录内')
    before = source.stat()
    target = cache / (source.stem+'__agent_'+uuid.uuid4().hex[:12]+source.suffix)
    digest = hashlib.sha256()
    try:
        with source.open('rb') as inp, target.open('xb') as out:
            while data := inp.read(4*1024*1024):
                digest.update(data)
                out.write(data)
        after = source.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
            raise OSError('文件仍在写入，稍后重试')
        with target.open('rb') as copied:
            if hashlib.file_digest(copied,'sha256').hexdigest()!=digest.hexdigest():
                raise OSError('副本校验失败')
        return target,digest.hexdigest()
    except Exception:
        target.unlink(missing_ok=True)
        raise


def run(config, local, stop, report, approve):
    source = Path(config['share_root']) / config['folder']
    shared = Path(config['share_root']) / '.qianchuan-agent' / config['account_id']
    shared.mkdir(parents=True,exist_ok=True)
    local = Path(local)
    cache = local/'staging'
    cache.mkdir(parents=True,exist_ok=True)
    observed = {}
    ignored = {}
    while not stop.is_set():
        browser = None
        try:
            with AccountLock(shared/'account.lock') as lock:
                meta_path = shared/'account.json'
                identity = {key:config[key] for key in ('account_id','account_name','folder','protected')}
                if meta_path.exists():
                    existing = json.loads(meta_path.read_text(encoding='utf-8'))
                    if existing!=identity:
                        raise ValueError('账户配置与共享登记不一致，请由管理员核对，不能降低主户保护')
                else:
                    atomic_json(meta_path,identity)
                ledger = Ledger(shared/'receipts',lock)
                browser = Browser(local/'edge-profile',config)
                report('已取得账户处理权；请在专用Edge窗口确认登录')
                while not stop.is_set():
                    lock.check()
                    try:
                        browser.check()
                    except Exception:
                        report('等待手动登录并进入所配置账户的视频库；暂不领取上传任务')
                        stop.wait(5)
                        continue
                    # Uncertain records are verification-only. Never replay mutations.
                    pending = list(ledger.pending())
                    if pending:
                        report('有 '+str(len(pending))+' 条结果待核对，核对期间不新增上传')
                        for digest,row in pending:
                            if stop.is_set():
                                break
                            material_id = browser.find(row['remote_name']) if row.get('remote_name') else None
                            if material_id:
                                row.update(status='success',material_id=material_id,verified_at=datetime.now(TZ).isoformat())
                                ledger.put(digest,row)
                                report('已核实入库：'+row.get('name',digest))
                        if list(ledger.pending()):
                            stop.wait(60)
                            continue
                    worked = False
                    for file,stat in files_under(source):
                        if stop.is_set():
                            break
                        signature = (stat.st_size,stat.st_mtime_ns)
                        if ignored.get(str(file))==signature:
                            continue
                        old = observed.get(str(file))
                        if not old or old[0]!=signature:
                            observed[str(file)] = (signature,time.monotonic())
                            continue
                        if time.monotonic()-old[1]<10:
                            continue
                        staged,digest = snapshot(file,source,cache)
                        receipt = ledger.get(digest)
                        if receipt:
                            staged.unlink(missing_ok=True)
                            if receipt['status']=='invalid':
                                ignored[str(file)] = signature
                            if receipt['status']=='success':
                                try:
                                    delete_verified(file,source,digest,receipt,lock)
                                    report('已确认入库并删除原片：'+file.name)
                                except (OSError,ValueError) as error:
                                    report('原片保留，清理待重试：'+str(error))
                            continue
                        try:
                            validate(staged)
                        except ValueError as error:
                            ledger.put(digest,{'status':'invalid','name':file.name,'reason':str(error)})
                            ignored[str(file)] = signature
                            staged.unlink(missing_ok=True)
                            report('原片保留，参数需处理：'+file.name+'；'+str(error))
                            continue
                        day = datetime.now(TZ).strftime('%Y-%m-%d')
                        quota_path = shared/('quota-'+day+'.json')
                        quota = json.loads(quota_path.read_text()) if quota_path.exists() else {'reserved':0}
                        if config['protected']:
                            quota['reserved'] = max(quota['reserved'],browser.daily_count(day))
                            if quota['reserved']>=699 and not approve(file.name,quota['reserved']):
                                staged.unlink(missing_ok=True)
                                report('主户达到699条，未获得本条额外批准；暂停')
                                stop.set()
                                break
                        if day!=datetime.now(TZ).strftime('%Y-%m-%d'):
                            staged.unlink(missing_ok=True)
                            continue
                        lock.check()
                        quota['reserved']+=1
                        atomic_json(quota_path,quota)
                        receipt = {'status':'uncertain','name':file.name,'source':str(file),'remote_name':staged.name,'created_at':datetime.now(TZ).isoformat()}
                        ledger.put(digest,receipt)
                        report('上传中：'+file.name)
                        material_id = browser.upload(staged,lock,stop,report)
                        receipt.update(status='success',material_id=material_id,verified_at=datetime.now(TZ).isoformat())
                        ledger.put(digest,receipt)
                        report('已确认入库：'+file.name+'，素材ID '+material_id)
                        try:
                            delete_verified(file,source,digest,receipt,lock)
                            report('已删除已入库原片：'+file.name)
                        except (OSError,ValueError) as error:
                            report('原片保留，清理待重试：'+str(error))
                        staged.unlink(missing_ok=True)
                        worked = True
                        break
                    if not worked:
                        stop.wait(3)
        except AccountBusy as error:
            report(str(error))
            stop.wait(15)
        except Exception as error:
            report('需要处理：'+str(error))
            stop.wait(30)
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
