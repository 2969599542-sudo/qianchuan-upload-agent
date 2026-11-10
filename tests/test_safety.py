import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from agent.state import AccountBusy, AccountLock, Ledger, delete_verified
from agent.worker import snapshot
from agent.migration import import_legacy


def test_two_processes_cannot_claim_same_account(tmp_path):
    path = tmp_path/'account.lock'
    with AccountLock(path):
        code = '''from agent.state import AccountLock,AccountBusy
import sys
try:
 with AccountLock(sys.argv[1]): print('CLAIMED')
except AccountBusy: print('BUSY')
'''
        result = subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True,text=True,check=True)
        assert result.stdout.strip()=='BUSY'
    with AccountLock(path):
        pass


def test_receipt_required_and_changed_file_preserved(tmp_path):
    original = tmp_path/'movie.mp4'
    original.write_bytes(b'original')
    digest = hashlib.sha256(b'original').hexdigest()
    with AccountLock(tmp_path/'account.lock') as lock:
        with pytest.raises(ValueError):
            delete_verified(original,tmp_path,digest,{'status':'uncertain'},lock)
        assert original.exists()
        receipt = {'status':'success','material_id':'123'}
        original.write_bytes(b'replacement')
        with pytest.raises(ValueError):
            delete_verified(original,tmp_path,digest,receipt,lock)
        assert original.read_bytes()==b'replacement'
        original.write_bytes(b'original')
        delete_verified(original,tmp_path,digest,receipt,lock)
        assert not original.exists()


def test_snapshot_digest_and_path_boundary(tmp_path):
    source = tmp_path/'source'
    cache = tmp_path/'cache'
    source.mkdir();cache.mkdir()
    video = source/'movie.mp4'
    video.write_bytes(b'video'*10000)
    staged,digest = snapshot(video,source,cache)
    assert staged.read_bytes()==video.read_bytes()
    assert digest==hashlib.sha256(video.read_bytes()).hexdigest()
    outside = tmp_path/'outside.mp4'
    outside.write_bytes(b'other')
    with pytest.raises(ValueError):
        snapshot(outside,source,cache)


def test_uncertain_receipt_survives_new_owner(tmp_path):
    digest = 'a'*64
    with AccountLock(tmp_path/'account.lock') as lock:
        ledger = Ledger(tmp_path/'receipts',lock)
        ledger.put(digest,{'status':'uncertain','remote_name':'unique.mp4'})
    with AccountLock(tmp_path/'account.lock') as lock:
        ledger = Ledger(tmp_path/'receipts',lock)
        assert list(ledger.pending())==[(digest,{'status':'uncertain','remote_name':'unique.mp4'})]


def test_legacy_batch_preserves_unresolved_names(tmp_path):
    legacy = tmp_path/'old'
    legacy.mkdir()
    (legacy/'upload-config.json').write_text(json.dumps({'advertiser_id':'123'}))
    db = sqlite3.connect(legacy/'upload-state.sqlite')
    db.execute('create table uploads(hash,name,status,detail)')
    db.execute('create table daily(day,count)')
    db.execute('insert into daily values(?,?)',('2026-11-10',699))
    db.execute('insert into uploads values(?,?,?,?)',('b'*64,'old.mp4','success',json.dumps([{'id':'456'}])))
    job={'batch':[{'hash':'a'*64,'staged':str(tmp_path/'unique.mp4'),'source':'original.mp4'}]}
    db.execute('insert into uploads values(?,?,?,?)',('a'*64,'original.mp4','uncertain',json.dumps(job)))
    db.commit();db.close()
    root = tmp_path/'share'
    result = import_legacy(legacy,root,'123')
    assert result=={'success':1,'uncertain':1,'skipped':0}
    folder = root/'.qianchuan-agent'/'123'
    row=json.loads((folder/'receipts'/('a'*64+'.json')).read_text())
    assert row['remote_name']=='unique.mp4' and row['status']=='uncertain'
    assert json.loads((folder/'quota-2026-11-10.json').read_text())['reserved']==699
    assert import_legacy(legacy,root,'123')['skipped']==2
