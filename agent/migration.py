"""Explicit local import; no company configuration is shipped in the release."""
import json
from pathlib import Path
import re
import sqlite3

from .state import AccountLock, Ledger, atomic_json


def import_legacy(folder, share_root, account_id):
    folder, share_root = Path(folder),Path(share_root)
    cfg = json.loads((folder/'upload-config.json').read_text(encoding='utf-8'))
    if str(cfg['advertiser_id'])!=account_id:
        raise ValueError('旧记录账户与当前账户不一致')
    shared = share_root/'.qianchuan-agent'/account_id
    counts = {'success':0,'uncertain':0,'skipped':0}
    with AccountLock(shared/'account.lock') as lock:
        ledger = Ledger(shared/'receipts',lock)
        db = sqlite3.connect(f'{(folder/"upload-state.sqlite").as_uri()}?mode=ro',uri=True)
        try:
            for digest,name,status,detail in db.execute('SELECT hash,name,status,detail FROM uploads'):
                if ledger.get(digest):
                    counts['skipped']+=1
                    continue
                if status not in ('success','uncertain'):
                    continue
                try:
                    data = json.loads(detail)
                except (ValueError,TypeError):
                    data = {}
                receipt = {'status':status,'name':name,'legacy':True}
                if status=='success':
                    ids = [str(item.get('id')) for item in data if isinstance(item,dict) and item.get('id')] if isinstance(data,list) else []
                    if not ids:
                        receipt['status']='uncertain'
                    else:
                        receipt['material_id']=ids[0]
                if status=='uncertain' and isinstance(data,dict):
                    item = next((i for i in data.get('batch',[]) if i.get('hash')==digest),data)
                    if item.get('staged'):
                        receipt['remote_name']=Path(item['staged']).name
                    if item.get('source'):
                        receipt['source']=item['source']
                if receipt['status']=='uncertain' and not receipt.get('remote_name'):
                    files = list((folder/'staging'/digest).glob('*'))
                    if len(files)==1:
                        receipt['remote_name']=files[0].name
                ledger.put(digest,receipt)
                counts[receipt['status']]+=1
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='daily'").fetchone():
                for day,count in db.execute('SELECT day,count FROM daily'):
                    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):
                        continue
                    path = shared/('quota-'+day+'.json')
                    previous = json.loads(path.read_text()) if path.exists() else {'reserved':0}
                    atomic_json(path,{'reserved':max(int(count),previous['reserved'])})
        finally:
            db.close()
    return counts
