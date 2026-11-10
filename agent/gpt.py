"""Optional, read-only GPT diagnostics. No browser, shell or deletion tools."""
import ctypes
from ctypes import wintypes
import json
import re
import threading
import time
import urllib.request
import urllib.error

from .state import atomic_json


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def protect(data, decrypt=False):
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise RuntimeError('无法访问本机加密密钥，请重新配置GPT')
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


def save_settings(base, key, model, automatic):
    model = model.strip()
    if not re.fullmatch(r'[a-zA-Z0-9._:-]{1,100}', model):
        raise ValueError('请输入有效模型名称')
    old = settings(base)
    if key.strip():
        secret = key.strip()
        if not secret.isascii() or any(c.isspace() for c in secret):
            raise ValueError('API密钥格式有误')
        encrypted = protect(secret.encode()).hex()
    else:
        encrypted = old.get('encrypted_key', '')
    if not encrypted:
        raise ValueError('首次设置请填写OpenAI API密钥')
    atomic_json(base/'gpt-settings.json', {'model': model, 'automatic': bool(automatic),
                                         'encrypted_key': encrypted})


def settings(base):
    path = base/'gpt-settings.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def category(text):
    # Allowlisted categories only: raw filenames, paths, IDs and logs never leave the PC.
    if '待核对' in text or '尚未查到素材ID' in text:
        return '上传结果未知，需要核对素材ID，已暂停新增上传'
    if '参数' in text:
        return '视频参数不合格或元数据读取失败，原片保留'
    if '登录' in text or '账户配置' in text:
        return '登录状态或账户身份需要人工核对'
    if '699' in text:
        return '主户已达到每日699条自动额度，需要逐条人工确认'
    if '清理待重试' in text:
        return '入库已确认，但原片删除失败，原片保留'
    if '需要处理' in text:
        return '上传程序发生异常，具体原因未提供；不能断言是网络问题'
    return '一般运行状态；没有足够信息判断异常原因'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request_advice(base, categories):
    conf = settings(base)
    if not conf.get('encrypted_key'):
        raise RuntimeError('请先打开GPT设置，填写API密钥')
    key = protect(bytes.fromhex(conf['encrypted_key']), decrypt=True).decode()
    payload = {'model': conf['model'], 'store': False, 'max_output_tokens': 600,
               'instructions': '你是千川上传助手的只读诊断顾问。用简短中文说明可能原因及下一步。仅有分类摘要，不能声称已检查网页、网速或已执行操作。不得建议绕过账户校验、共享锁、699条人工批准、删除凭据要求；结果未知必须先核实素材ID，不能重传或删除记录。不要给出执行代码。',
               'input': json.dumps({'recent_status_categories': categories[-8:]}, ensure_ascii=False)}
    request = urllib.request.Request('https://api.openai.com/v1/responses',
        data=json.dumps(payload).encode(), headers={'Authorization': 'Bearer '+key,
        'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=45) as response:
            data = json.load(response)
    except urllib.error.HTTPError as e:
        messages = {401: 'API密钥无效或已失效', 403: 'API访问被拒绝，请检查账户权限',
                    404: '模型不可用，请检查模型名称和权限', 429: 'API额度不足或请求限流'}
        raise RuntimeError(messages.get(e.code, 'GPT服务返回错误（HTTP '+str(e.code)+'）')) from None
    except (OSError, ValueError):
        raise RuntimeError('GPT连接失败或响应无效，请检查网络后重试；上传规则不受影响') from None
    texts = [part.get('text', '') for item in data.get('output', []) if item.get('type') == 'message'
             for part in item.get('content', []) if part.get('type') == 'output_text']
    if not texts or data.get('status') not in ('completed', None):
        raise RuntimeError('GPT未完成诊断，请稍后重试')
    return '\n'.join(texts)[:6000]


class Advisor:
    def __init__(self, base, deliver):
        self.base, self.deliver = base, deliver
        self.lock = threading.Lock()
        self.recent = []
        self.busy = False
        self.last_auto = float('-inf')

    def observe(self, text):
        value = category(text)
        with self.lock:
            self.recent = (self.recent+[value])[-8:]
        try:
            enabled = settings(self.base).get('automatic', False)
        except (OSError, ValueError):
            enabled = False
        if enabled and any(s in text for s in ('需要处理', '待核对', '参数需处理', '清理待重试')):
            self.ask(automatic=True)

    def ask(self, automatic=False):
        with self.lock:
            if self.busy or (automatic and time.monotonic()-self.last_auto < 300):
                return False
            self.busy = True
            if automatic:
                self.last_auto = time.monotonic()
            snapshot = list(self.recent) or ['连接测试：简短确认诊断服务可用，不推断上传任务进度']
        def job():
            try:
                result = 'GPT建议（未执行任何操作）：\n'+request_advice(self.base, snapshot)
            except Exception as error:
                result = str(error) if isinstance(error, RuntimeError) else 'GPT配置读取失败，请重新设置'
            finally:
                with self.lock:
                    self.busy = False
            self.deliver(result)
        threading.Thread(target=job, daemon=True).start()
        return True
