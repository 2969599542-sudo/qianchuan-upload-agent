import re
import time
from playwright.sync_api import sync_playwright, expect


class Browser:
    def __init__(self, profile, config):
        self.config = config
        self.url = 'https://qianchuan.jinritemai.com/tools/creative-management/video-library?aavid=' + config['account_id']
        self.runtime = sync_playwright().start()
        try:
            self.context = self.runtime.chromium.launch_persistent_context(str(profile),channel='msedge',headless=False)
        except Exception:
            self.runtime.stop()
            raise
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.goto(self.url,wait_until='domcontentloaded')

    def check(self):
        from urllib.parse import urlparse, parse_qs
        if parse_qs(urlparse(self.page.url).query).get('aavid') != [self.config['account_id']]:
            raise RuntimeError('请在专用Edge窗口登录并进入正确账户的视频库')
        expect(self.page.locator('body')).to_contain_text('ID：'+self.config['account_id'],timeout=5000)
        expect(self.page.locator('body')).to_contain_text(self.config['account_name'],timeout=5000)

    def find(self, name):
        self.page.goto(self.url,wait_until='domcontentloaded')
        self.check()
        search = self.page.get_by_placeholder('请输入视频名称/ID搜索',exact=True)
        search.fill(name)
        search.press('Enter')
        try:
            expect(self.page.get_by_text(name,exact=True)).to_be_visible(timeout=12000)
        except Exception:
            return None
        for text in self.page.locator('tr').all_inner_texts():
            match = re.search(r'ID[：:]\s*(\d+)',text)
            if name in text and match:
                return match.group(1)
        return None

    def daily_count(self, day):
        self.page.goto(self.url,wait_until='domcontentloaded')
        self.check()
        search = self.page.get_by_placeholder('请输入视频名称/ID搜索',exact=True)
        search.fill('')
        search.press('Enter')
        self.page.wait_for_function("()=>Array.from(document.querySelectorAll('tr')).some(e=>/ID[：:]\\s*\\d+/.test(e.innerText))")
        ids, previous = set(), '9999'
        for number in range(1,37):
            if number>1:
                old = self.page.locator('tr').all_inner_texts()
                jump = self.page.locator('.ovui-page-jumper input')
                jump.fill(str(number))
                jump.press('Enter')
                expect(self.page.locator('.ovui-page-turner__item--active')).to_have_text(str(number))
                self.page.wait_for_function("old=>JSON.stringify(Array.from(document.querySelectorAll('tr')).map(e=>e.innerText))!==JSON.stringify(old)&&!document.body.innerText.includes('加载中...')",arg=old)
            rows = [t for t in self.page.locator('tr').all_inner_texts() if re.search(r'ID[：:]\s*\d+',t)]
            if not rows:
                raise RuntimeError('无法核实当日配额，暂不上传')
            for text in rows:
                stamp = re.search(r'20\d\d-\d\d-\d\d \d\d:\d\d:\d\d',text)
                if not stamp or stamp[0][:10]>previous:
                    raise RuntimeError('列表排序无法确认，暂不上传')
                previous = stamp[0][:10]
                if previous<day:
                    return len(ids)
                if previous!=day:
                    raise RuntimeError('上传列表日期异常')
                ids.add(re.search(r'ID[：:]\s*(\d+)',text)[1])
            if len(ids)>=699 or len(rows)<20:
                return len(ids)
        raise RuntimeError('无法核实当日配额')

    def upload(self, staged, lock, stop, report):
        self.page.goto(self.url,wait_until='domcontentloaded')
        self.check()
        lock.check()
        self.page.get_by_role('button',name='上传视频',exact=True).click()
        with self.page.expect_file_chooser() as chooser:
            self.page.get_by_text('点击上传',exact=True).click()
        chooser.value.set_files(str(staged))
        deadline = time.monotonic()+1800
        while time.monotonic()<deadline:
            if stop.wait(3):
                raise RuntimeError('已停止；本条结果留待核对，不能直接重传')
            lock.check()
            self.check()
            text = self.page.locator('body').inner_text()
            if staged.name not in text:
                continue
            if re.search(r'已选择\s*1\s*/\s*100',text):
                lock.check()
                self.page.get_by_role('button',name='确定',exact=True).filter(visible=True).click()
                expect(self.page.get_by_text('点击上传',exact=True)).to_be_hidden(timeout=45000)
                for _ in range(8):
                    material_id = self.find(staged.name)
                    if material_id:
                        return material_id
                    if stop.wait(5):
                        break
                raise RuntimeError('已提交，尚未查到素材ID；保留原片和待核对记录')
        raise RuntimeError('传输超时；保留待核对记录，不自动重复提交')

    def close(self):
        try:
            self.context.close()
        finally:
            self.runtime.stop()
