"""Account-scoped SMB exclusion and durable per-content receipts.

Never expire or steal a lock by wall-clock time. An uncertain upload is never
automatically uploaded a second time, even after the operating-system lock ends.
"""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import uuid


class AccountBusy(RuntimeError):
    pass


class AccountLock:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.k = ctypes.WinDLL('kernel32', use_last_error=True)
        self.k.CreateFileW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        self.k.CreateFileW.restype = wintypes.HANDLE
        self.k.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        self.k.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k.WriteFile.argtypes = [wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p]
        self.handle = self.k.CreateFileW(str(self.path),0xC0000000,0,None,4,0x80000000,None)
        if self.handle == ctypes.c_void_p(-1).value:
            self.handle = None
            raise AccountBusy('该账户正由另一台电脑处理，或共享盘暂不可写')
        self.check()
        return self

    def check(self):
        written = wintypes.DWORD()
        if not self.handle or not self.k.WriteFile(self.handle,b'.',1,ctypes.byref(written),None) or not self.k.FlushFileBuffers(self.handle):
            raise OSError('共享盘连接或账户锁已失效，停止提交和删除')

    def __exit__(self, *args):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('w', encoding='utf-8') as out:
        json.dump(data,out,ensure_ascii=False,indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp,path)


class Ledger:
    def __init__(self, folder, lock):
        self.folder, self.lock = Path(folder), lock
        self.folder.mkdir(parents=True, exist_ok=True)

    def get(self, digest):
        path = self.folder / (digest + '.json')
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

    def put(self, digest, value):
        if len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('无效内容摘要')
        self.lock.check()
        atomic_json(self.folder/(digest+'.json'),value)

    def pending(self):
        for path in self.folder.glob('*.json'):
            row = json.loads(path.read_text(encoding='utf-8'))
            if row.get('status') == 'uncertain':
                yield path.stem, row


def delete_verified(file, source_root, digest, receipt, lock):
    """Only disposition-delete the exact, exclusively opened, verified content."""
    import msvcrt
    file, source_root = Path(file), Path(source_root)
    if receipt.get('status')!='success' or not receipt.get('material_id'):
        raise ValueError('尚无明确入库凭据，不能删除')
    if not file.resolve().is_relative_to(source_root.resolve()) or file.is_symlink():
        raise ValueError('源文件路径不属于本账户')
    lock.check()
    k = ctypes.WinDLL('kernel32',use_last_error=True)
    k.CreateFileW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    k.CreateFileW.restype = wintypes.HANDLE
    k.SetFileInformationByHandle.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
    handle = k.CreateFileW(str(file),0x80010000,0,None,3,0x80,None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    fd = msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
    with os.fdopen(fd,'rb') as content:
        if hashlib.file_digest(content,'sha256').hexdigest()!=digest:
            raise ValueError('源文件已变化，保留原片')
        lock.check()
        delete = ctypes.c_ubyte(1)
        if not k.SetFileInformationByHandle(handle,4,ctypes.byref(delete),ctypes.sizeof(delete)):
            raise ctypes.WinError(ctypes.get_last_error())
