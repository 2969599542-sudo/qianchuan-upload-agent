import io
import json
import threading
import urllib.error
import pytest
from agent import gpt


def test_encrypted_key_roundtrip_and_keep(tmp_path):
    secret = 'test-secret-not-real'
    gpt.save_settings(tmp_path, secret, 'gpt-4.1-mini', False)
    raw = (tmp_path/'gpt-settings.json').read_text()
    assert secret not in raw
    conf = gpt.settings(tmp_path)
    assert gpt.protect(bytes.fromhex(conf['encrypted_key']), True).decode() == secret
    gpt.save_settings(tmp_path, '', 'gpt-4.1-mini', True)
    assert gpt.settings(tmp_path)['encrypted_key'] == conf['encrypted_key']


def test_api_payload_does_not_send_raw_logs(tmp_path, monkeypatch):
    gpt.save_settings(tmp_path, 'test-key', 'gpt-4.1-mini', False)
    class Fake:
        def open(self, request, timeout):
            assert request.full_url == 'https://api.openai.com/v1/responses'
            data = json.loads(request.data)
            assert data['store'] is False
            assert 'tools' not in data
            assert 'SECRET' not in data['input']
            assert '192.168' not in data['input']
            return io.BytesIO(json.dumps({'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'先核实入库'}]}]}).encode())
    monkeypatch.setattr(gpt.urllib.request, 'build_opener', lambda *a: Fake())
    result = gpt.request_advice(tmp_path, [gpt.category(r'待核对 SECRET \\192.168.5.5\secret.mp4')])
    assert result == '先核实入库'


def test_api_error_never_echoes_secret(tmp_path, monkeypatch):
    gpt.save_settings(tmp_path, 'test-key', 'gpt-4.1-mini', False)
    class Fake:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError('url',401,'secret details',{},None)
    monkeypatch.setattr(gpt.urllib.request,'build_opener',lambda *a:Fake())
    with pytest.raises(RuntimeError,match='密钥无效') as error:
        gpt.request_advice(tmp_path,[])
    assert 'secret' not in str(error.value)


def test_auto_throttle_and_manual_request(tmp_path, monkeypatch):
    done = threading.Event()
    monkeypatch.setattr(gpt,'request_advice',lambda *a:'test advice')
    advisor = gpt.Advisor(tmp_path,lambda text:done.set())
    assert advisor.ask(automatic=True)
    assert done.wait(2)
    assert not advisor.ask(automatic=True)
    assert advisor.ask()


def test_missing_key_and_invalid_model(tmp_path):
    with pytest.raises(ValueError):
        gpt.save_settings(tmp_path,'','gpt-4.1-mini',False)
    with pytest.raises(ValueError):
        gpt.save_settings(tmp_path,'test','https://another-provider',False)
    with pytest.raises(RuntimeError,match='先打开GPT设置'):
        gpt.request_advice(tmp_path,[])
