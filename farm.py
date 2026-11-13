#!/usr/bin/env python3
"""通过 ADB 进入淘宝/支付宝芭芭农场，执行已验证的浏览和领奖任务。"""

import argparse
import atexit
import datetime as dt
import json
import logging
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
import time
import tempfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError


PACKAGE = "com.taobao.taobao"
APPS = {
    "taobao": {"package": PACKAGE, "name": "淘宝", "list_entries": ("集肥料",)},
    "alipay": {"package": "com.eg.android.AlipayGphone", "name": "支付宝",
               "list_entries": ("领肥料", "任务列表")},
}
# 只执行实机验证过的任务；不要把“买好物”等购买任务加入此列表。
TASKS = {
    "selected": ("看严选推荐商品", "发现精选好物", "搜一搜你心仪的宝贝", "搜一搜你喜欢的商品"),
    "browse15": ("逛精选好物",),
    "gift": ("领肥料礼包",),
    "coins": (),  # 活动金额会变化，下面用限定格式识别标题。
    "ip": ("天天签到免费拿IP周边",),
    "domestic": ("浏览国货严选红包抵扣",),
    "saving_card": ("去省钱卡领红包",),
    "takeaway": ("领百亿外卖红包",),  # 实测为5秒浏览任务，复用浏览及返回核验流程。
}
COINS_TASK = re.compile(r"^双11来淘金币狂享\d+(?:\.\d+)?[亿万]$")
# 推荐浏览会改为具体商品词；仍须在解析任务行时核验浏览说明、奖励与操作按钮。
DYNAMIC_SELECTED_TASK = re.compile(r"^看看#[^#()（）\r\n]+$")
PROGRESS = re.compile(r"^(.+?)[(（](\d+)/(\d+)[)）]$")
SAVING_TASK = re.compile(r"^\s*逛一逛省钱会场当前进度(\d+)，总进度(\d+)")
ALIPAY_TASKS = {
    "home_farm": ("从支付宝首页访问农场", re.compile(
        r"^\s*从支付宝首页访问农场当前进度(\d+)，总进度(\d+)")),
    "gold_visit": ("逛支付宝金豆夺宝", re.compile(
        r"^\s*逛支付宝金豆夺宝当前进度(\d+)，总进度(\d+)")),
    "home_gold": ("从支付宝首页访问金豆", re.compile(
        r"^\s*从支付宝首页访问金豆当前进度(\d+)，总进度(\d+)")),
    "instant": ("逛好物立得肥料", re.compile(
        r"^\s*(?:逛好物|浏览好物)立得\d+肥料当前进度(\d+)，总进度(\d+)")),
    "goodies": ("逛好物最高得肥料", re.compile(
        r"^\s*逛好物最高得\d+肥料当前进度(\d+)，总进度(\d+)")),
    "featured": ("看精选商品得肥料", re.compile(
        r"^\s*看精选商品得肥料当前进度(\d+)，总进度(\d+)")),
    "saving": ("逛一逛省钱会场", SAVING_TASK),
    "insurance": ("搜一搜「车险」", re.compile(
        r"^\s*搜一搜[「“\"]车险[」”\"]当前进度(\d+)，总进度(\d+)")),
}
SAVING_PROGRESS = re.compile(r"点3个商品，得1000肥料[（(](\d+)/(\d+)[）)]")
MAX_TASK_ROUNDS = 100  # 只限制任务尝试次数，不限制完成后的最终列表检查。
MAX_GOODIES_CARDS = 10  # 奖励现身卡片每张100，最多核验10张，不按点击动作推算到账。
MAX_CARD_SCAN_SWIPES = 30
MAX_CARD_IDLE_SWIPES = 6
BROWSE_READ_INTERVAL = 1.0  # 采集尚未结束时至多静候1秒，再推进已验证页面的浏览。
MAX_ACTIVE_READ_SECONDS = 20  # 页面长时间无法核验时停止操作，不无限维持旧状态。


class TaskUnavailable(RuntimeError):
    """任务未出现或不可定位；携带最后确认的列表，供调度器继续其他任务。"""
    def __init__(self, message, page):
        super().__init__(message)
        self.page = page


class TaskVerificationFailed(RuntimeError):
    """返回后的进度或领奖核验失败；只有再次感知确认任务列表后才允许继续其他任务。"""


def bounds(node):
    values = list(map(int, re.findall(r"-?\d+", node.get("bounds", ""))))
    return tuple(values) if len(values) == 4 else (0, 0, 0, 0)


def find_adb():
    """优先显式配置和 PATH，再查找 Android SDK 常见安装目录。"""
    configured = os.environ.get("ADB_PATH")
    if configured:
        executable = shutil.which(os.path.expanduser(configured))
        if executable:
            return executable
        raise RuntimeError(f"ADB_PATH 指向的程序不存在或没有执行权限：{configured}")
    executable = shutil.which("adb")
    if executable:
        return executable
    roots = [Path(os.environ[key]).expanduser() for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT")
             if os.environ.get(key)]
    home = Path.home()
    roots.extend((home / "Library/Android/sdk", home / "Android/Sdk",
                  home / "AppData/Local/Android/Sdk"))
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "Android/Sdk")
    for root in roots:
        candidate = root / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise RuntimeError("未找到 adb；请安装 Android platform-tools，或设置 ADB_PATH 为 adb 的完整路径")


def ensure_xml_runtime():
    """在操作设备前实测 XML 解析；当前解释器损坏时切换到已通过探测的 Python。"""
    try:
        ET.fromstring("<runtime-check/>")
        return
    except ImportError as exc:
        reason = str(exc)
    # 本项目仅用标准库，切换解释器不依赖虚拟环境内的第三方包。
    candidates = ["/usr/bin/python3"]
    candidates.extend(shutil.which(name) for name in
                      ("python3.14", "python3.13", "python3.12", "python3.11", "python3.10", "python3.9"))
    checked = {os.path.realpath(sys.executable)}
    probe = "import xml.etree.ElementTree as E; E.fromstring('<runtime-check/>')"
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        identity = os.path.realpath(candidate)
        if identity in checked:
            continue
        checked.add(identity)
        try:
            result = subprocess.run([candidate, "-c", probe], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode:
            continue
        print(f"当前 Python 的 XML 模块异常，自动切换至 {candidate}", file=sys.stderr, flush=True)
        # 保留脚本参数、工作目录及环境；新解释器会再次自检，之后才连接 ADB。
        os.execv(candidate, [candidate, str(Path(__file__).resolve()), *sys.argv[1:]])
        return
    raise RuntimeError(f"Python XML/expat 模块异常，请修复 Python 或换用可解析 XML 的解释器：{reason}")


class Page:
    def __init__(self, xml, package=PACKAGE):
        self.package = package
        self.root = ET.fromstring(xml)
        self.nodes = list(self.root.iter("node"))
        self.parents = {child: parent for parent in self.root.iter() for child in parent}
        self.texts = [n.get("text", "") for n in self.nodes]
        self.screen_rows = None
        self.screen_signature = None
        self.screen_nodes = set()
        self.screen_issues = []  # 记录截图中无法唯一绑定的入口，供扫描汇总解释漏跑原因。
        self.browse_motion_since_capture = False  # 采集期间滚动过的卡片坐标不得直接点击。
        # 顶层窗口才代表屏幕：横向轮播的屏外子节点实测可延伸到 x=41640。
        # 用所有节点的最大边界会把点击压缩到屏幕左侧，并误判屏外按钮可见。
        windows = [n for n in self.root.findall("node")
                   if bounds(n)[2] > bounds(n)[0] and bounds(n)[3] > bounds(n)[1]]
        viewport_nodes = windows or self.nodes
        self.width = max((bounds(n)[2] for n in viewport_nodes), default=0)
        self.height = max((bounds(n)[3] for n in viewport_nodes), default=0)

    def has(self, text):
        return any(text in s for s in self.texts)

    @property
    def taobao(self):
        return any(n.get("package") == PACKAGE for n in self.nodes)

    @property
    def in_app(self):
        return any(n.get("package") == self.package for n in self.nodes)

    @property
    def task_list(self):
        if self.reward_popup:
            return False
        if self.package == APPS["alipay"]["package"]:
            # 首页访问引导覆盖列表时，背景节点仍存在，不能按背景任务坐标操作。
            if any(self.entry(label) is not None for label in ("从常用访问金豆", "从常用访问农场")):
                return False
            return self.in_app and self.has("做任务集肥料") and self.has("关闭做任务集肥料弹窗")
        return self.has("肥料明细") and any(
            n.get("text") in ("去完成", "去逛逛", "去领取", "去开启", "已领取", "已完成")
            or re.fullmatch(r"\d+点可领", n.get("text", "")) for n in self.nodes
        )

    @property
    def reward_popup(self):
        return (self.taobao and self.has("昨日施肥奖励可领取")
                and self.has("有一笔肥料待领取") and self.has("立即领取"))

    def reward_popup_close(self):
        """实机弹窗的关闭按钮无文字，限定在奖励容器内且位于领取按钮下方。"""
        if not self.reward_popup:
            return None
        title = self.exact("昨日施肥奖励可领取")
        parent = self.parents.get(title) if title is not None else None
        while parent is not None:
            claim = next((n for n in parent.iter("node") if n.get("text") == "立即领取"
                          and self.visible(n)), None)
            if claim is not None:
                closes = [n for n in parent.iter("node")
                          if n.get("class") == "android.widget.Button"
                          and n.get("clickable") == "true" and n.get("enabled") != "false"
                          and not n.get("text") and not n.get("content-desc")
                          and self.visible(n) and bounds(n)[1] >= bounds(claim)[3]]
                return closes[0] if len(closes) == 1 else None
            parent = self.parents.get(parent)
        return None

    def visible(self, node):
        x1, y1, x2, y2 = bounds(node)
        return 0 <= x1 < x2 <= self.width and 0 <= y1 < y2 <= self.height

    def exact(self, text):
        return next((n for n in self.nodes if n.get("text") == text
                     and self.visible(n)), None)

    def task_tappable(self, node):
        """任务入口须在父容器裁剪区域内，且点击中心不被推广横幅覆盖。"""
        if self.screen_rows is not None and node not in self.screen_nodes:
            return False
        if node is None or not self.visible(node) or node.get("enabled") == "false":
            return False
        x1, y1, x2, y2 = bounds(node)
        parent = self.parents.get(node)
        while parent is not None:
            left, top, right, bottom = bounds(parent)
            # 缺失 bounds 的结构节点不提供裁剪信息。
            if right > left and bottom > top and not (
                    left <= x1 < x2 <= right and top <= y1 < y2 <= bottom):
                return False
            parent = self.parents.get(parent)
        # 淘宝 WebView 偶尔将整个 ListView 报到面板上方；其子节点即使
        # 落在屏幕内也可能属于其他行。拒绝整棵异常列表，不能继续按旧坐标点。
        parent = self.parents.get(node)
        while parent is not None:
            if parent.get("class") == "android.widget.ListView":
                left, top, right, bottom = bounds(parent)
                ancestor = self.parents.get(parent)
                while ancestor is not None:
                    aleft, atop, aright, abottom = bounds(ancestor)
                    # 列表底部可能正常越出视口或存在 1px 取整差；这里只拒绝
                    # 已实测会造成整列错位的顶部越界，而非要求列表完整露出。
                    if aright > aleft and abottom > atop and top < atop - 2:
                        return False
                    ancestor = self.parents.get(ancestor)
            parent = self.parents.get(parent)
        x, y = (x1 + x2) / 2, (y1 + y2) / 2
        for overlay in self.nodes:
            if overlay.get("content-desc") != "跳转链接" or overlay.get("clickable") != "true":
                continue
            left, top, right, bottom = bounds(overlay)
            if left <= x <= right and top <= y <= bottom:
                return False
        return True

    def entry(self, label):
        return next((n for n in self.nodes
                     if n.get("package") == self.package
                     and (n.get("text") == label or n.get("content-desc") == label)
                     and n.get("enabled") != "false" and self.visible(n)), None)

    @property
    def alipay_home(self):
        """用原生首页标签及常用应用区域确认首页，不把小程序中的引导图当入口。"""
        return (self.package == APPS["alipay"]["package"] and self.in_app
                and any(n.get("resource-id") == "com.alipay.android.tablauncher:id/selected_fl"
                        and n.get("content-desc") == "首页" for n in self.nodes)
                and self.entry("扫一扫") is not None)

    def common_app(self, label):
        """首页仅使用常用应用区域的真实入口，兼容常用列表中的文字和图标副本。"""
        if not (self.alipay_home or self.has("最近常用应用")):
            return None
        return next((n for n in self.nodes if n.get("text") == label and self.visible(n)
                     and n.get("enabled") != "false"
                     and (self.has("最近常用应用") or n.get("resource-id", "").endswith(":id/tv_usual_name"))), None)

    @property
    def gold_venue(self):
        """金豆会场顶部为画布；用实机兑换区和支金豆浮条共同确认，只观察不兑换。"""
        return (self.package == APPS["alipay"]["package"] and self.in_app
                and not self.task_list and self.has("兑换明细")
                and "支金豆" in self.texts and "立即兑" in self.texts)

    @property
    def farm_home_service(self):
        """首页访问农场实测先进入服务引导，只有明确指向农场的入口才可点击。"""
        return (self.in_app and self.has("请访问首页服务")
                and "点击这里，访问「芭芭农场」" in self.texts
                and self.entry("芭芭农场") is not None)

    def apply_task_screen(self, screen):
        """用同一张截图的完整任务标题和右侧按钮替代列表的失真坐标。"""
        original = self.task_rows()
        width, height = screen["width"], screen["height"]
        def normalized(text):
            # OCR 常在中文之间插入空格，括号也可能识别为中文全角。
            return re.sub(r"\s+", "", text).translate(str.maketrans("（）", "()"))
        texts = [(normalized(r["text"]), r["bounds"]) for r in screen["rows"]
                 if r.get("confidence", 0) >= 0.5]
        # 截图必须仍是任务面板；否则不能拿刚才的 XML 去点已经切换的页面。
        markers = (("做任务集肥料",) if self.package == APPS["alipay"]["package"]
                   else ("肥料明细", "做任务赚肥料"))
        if not any(any(marker in t for marker in markers) for t, _ in texts):
            raise RuntimeError("截图未核验到任务面板，停止点击")
        # 保留未在截图中确认的 XML 任务供完成核验，但禁止按其旧坐标调度。
        self.screen_rows = list(original)
        self.screen_nodes.clear()
        panel_top = max(box[3] for text, box in texts
                        if any(marker in text for marker in markers))
        # OCR 边框会有几个像素抖动；只比较列表任务标题，排除背景轮播和时钟。
        self.screen_signature = tuple(sorted(((text, (box[1] + box[3]) // 2) for text, box in texts
                                              if box[1] >= panel_top and PROGRESS.fullmatch(text)),
                                             key=lambda item: (item[1], item[0])))
        for text, title_box in texts:
            match = PROGRESS.fullmatch(text)
            if match is None or title_box[1] < panel_top:
                continue
            name, current, total = match.groups()
            matches = [r for r in original if normalized(r["name"]) == name and r["total"] == int(total)]
            if not matches:
                continue
            # 奖励位于标题下方、下一个任务标题上方；不能拿相邻行金额解除身份歧义。
            next_top = min((box[1] for title, box in texts if PROGRESS.fullmatch(title)
                            and box[1] > title_box[3]), default=height)
            row_bottom = min(next_top, title_box[3] + height * 0.065)
            rewards = {t for t, box in texts if re.fullmatch(r"\+\d+", t)
                       and title_box[0] <= box[0] < width * 0.7
                       and title_box[1] <= (box[1] + box[3]) / 2 < row_bottom}
            if rewards:
                matches = [r for r in matches if len(rewards) == 1 and r.get("reward") in rewards]
            if len(matches) != 1:
                self.screen_issues.append({"task": name, "reason": "同行奖励冲突或任务身份不唯一"})
                continue
            # 只配对同一行右侧的操作文字；相邻行按钮和底部推广入口不能代替。
            buttons = [(t, box) for t, box in texts
                       if t in ("去完成", "去逛逛", "去领取", "已完成", "已领取", "继续逛", "领取")
                       or re.fullmatch(r"\d+点可领", t)]
            buttons = [(t, box) for t, box in buttons
                       if box[0] > width * 0.7 and box[0] > title_box[2]
                       and abs((box[1] + box[3] - title_box[1] - title_box[3]) / 2)
                       < height * 0.04]
            if len(buttons) != 1:
                self.screen_issues.append({"task": name, "reason": "同行任务按钮未能唯一识别"})
                continue
            row = dict(matches[0])
            row["current"] = int(current)
            for key, label, box in (("title", text, title_box),
                                    ("button", buttons[0][0], buttons[0][1])):
                # OCR 为截图像素，换算回当前 UI 尺寸后仍走统一触控比例换算。
                left, top, right, bottom = [int(v * scale) for v, scale in zip(
                    box, (self.width / width, self.height / height) * 2)]
                node = ET.Element("node", text=label, enabled="true", clickable="true",
                                  bounds=f"[{left},{top}][{right},{bottom}]")
                row[key] = node
                self.screen_nodes.add(node)
            self.screen_rows[original.index(matches[0])] = row

    def task_rows(self):
        if self.screen_rows is not None:
            return self.screen_rows
        if self.package != PACKAGE:
            rows = []
            for node in self.nodes:
                selected = next(((key, name, match) for key, (name, pattern) in ALIPAY_TASKS.items()
                                 if (match := pattern.match(node.get("text", ""))) is not None), None)
                if selected is None:
                    continue
                key, name, match = selected
                if key in ("goodies", "instant"):
                    name = node.get("text", "").split("当前进度", 1)[0].strip()
                parent = self.parents.get(node)
                button = next((n for n in parent.iter("node")
                               if n.get("text") in ("去完成", "去逛逛", "继续逛", "领取", "已领取", "已完成")
                               and n.get("clickable") == "true"), None) if parent is not None else None
                rows.append({"key": key, "name": name,
                             "current": int(match[1]), "total": int(match[2]),
                             "title": node, "button": button})
            return rows
        rows = []
        for node in self.nodes:
            match = PROGRESS.match(node.get("text", ""))
            if not match:
                continue
            name, current, total = match.groups()
            key = next((key for key, aliases in TASKS.items() if name in aliases), None)
            dynamic_selected = DYNAMIC_SELECTED_TASK.fullmatch(name) is not None
            if key is None and dynamic_selected:
                key = "selected"
            if key is None and COINS_TASK.fullmatch(name):
                key = "coins"
            if key is None:
                continue
            # 实测单次“逛精选好物”也会改名为搜索/推荐商品；按同一任务族和总次数归一。
            if key == "selected" and int(total) == 1:
                key = "browse15"
            # 向上寻找包含该标题和其操作按钮的最小共同容器。
            parent = self.parents.get(node)
            button = None
            while parent is not None:
                titles = [n for n in parent.iter("node")
                          if PROGRESS.match(n.get("text", ""))]
                if len(titles) > 1:
                    break
                button = next((n for n in parent.iter("node")
                               if (n.get("text") in ("去完成", "去逛逛", "已完成", "去领取", "已领取")
                                   or re.fullmatch(r"\d+点可领", n.get("text", "")))
                               and n.get("clickable") == "true"), None)
                if button is not None:
                    break
                parent = self.parents.get(parent)
            # 奖励必须来自标题与按钮的最小共同容器，不能取相邻任务的金额。
            rewards = [n.get("text") for n in parent.iter("node")
                       if re.fullmatch(r"\+\d+", n.get("text", ""))] if button is not None else []
            if dynamic_selected:
                # 不把商品广告或下单任务按“看看#”前缀放行；证据必须来自同一任务容器。
                texts = [n.get("text", "") for n in parent.iter("node")] if parent is not None else []
                if (button is None or button.get("text") not in ("去完成", "已完成", "已领取")
                        or len(rewards) != 1 or not 0 <= int(current) <= int(total) or int(total) < 1
                        or not any(re.fullmatch(r"浏览\d+秒得(?:奖励)?", text) for text in texts)):
                    continue
            rows.append({"key": key, "name": name, "current": int(current),
                         "reward": rewards[0] if len(rewards) == 1 else None,
                         "total": int(total), "title": node, "button": button})
        return rows

    @property
    def saving_venue(self):
        return self.in_app and self.has("领补贴-支付宝-芭芭农场")

    def saving_progress(self):
        if not self.saving_venue:
            return None
        for text in self.texts:
            match = SAVING_PROGRESS.search(text)
            if match:
                return int(match[1]), int(match[2])
        return None

    @property
    def saving_done(self):
        progress = self.saving_progress()
        return self.saving_venue and (
            progress == (3, 3) or self.has("恭喜！完成任务")
            or any(re.search(r"再点3个商品，领最高.*红包", s) for s in self.texts))

    def saving_products(self):
        if not self.saving_venue or self.saving_done:
            return []
        products = []
        for node in self.nodes:
            label = node.get("content-desc", "")
            if (not label.endswith(" 点我得肥料") or node.get("clickable") != "true"
                    or not self.visible(node)):
                continue
            # 排除只露出一小条的卡片，也要求奖励文字在屏幕内。
            cta = next((n for n in node.iter("node") if n.get("text") == "点我得肥料"
                        and self.visible(n)), None)
            if cta is not None:
                products.append(node)
        # 优先选择不含领券跳转的商品，但不依赖特定商品名。
        return sorted(products, key=lambda n: "专享优惠券" in n.get("content-desc", ""))

    def cancel_taobao_prompt(self):
        if not any("支付宝" in s and "想要打开" in s and "淘宝" in s for s in self.texts):
            return None
        return next((n for n in self.nodes if n.get("package") == "com.vivo.appfilter"
                     and n.get("content-desc") == "取消" and self.visible(n)), None)

    @property
    def saving_destination(self):
        return self.in_app and any(
            n.get("resource-id", "").endswith(":id/textView_title")
            and n.get("text") in ("商品详情", "双十一超级红包会场") for n in self.nodes)

    @property
    def insurance_search(self):
        return self.in_app and any(
            n.get("resource-id") == "com.alipay.mobile.antui:id/search_input_box"
            and n.get("text") in ("买车险", "车险") for n in self.nodes)

    @property
    def insurance_done(self):
        return self.insurance_search and "肥料奖励" in self.texts and "已发放" in self.texts

    def browse_state(self):
        # 国货严选浏览奖励实测直接弹出到账面板，不一定出现通用“任务完成”。
        # 三条信号必须同属一个面板，避免把商品广告中的下单奖励当成完成。
        if self.taobao:
            for node in self.nodes:
                if not re.fullmatch(r"\d+肥料已到账", node.get("text", "")):
                    continue
                parent = self.parents.get(node)
                texts = [n.get("text", "") for n in parent] if parent is not None else []
                if (any(re.fullmatch(r"本页下单再得\d+肥料", s) for s in texts)
                        and any(re.fullmatch(r"\d+s后奖励消失", s) for s in texts)):
                    return "done"
        # 购买文案仅作为浏览完成信号，绝不点击。
        if self.has("当前页下单") or self.has("当前页面下单") or self.has("任务完成"):
            return "done"
        if any(re.search(r"立即下单.*最高.*肥料", s) for s in self.texts):
            return "done"
        if self.has("滑动浏览") or any(re.search(r"\d+秒.*肥料|浏览.*\d+秒", s)
                                       for s in self.texts):
            return "browsing"
        if self.has("搜索有福利"):
            return "search"
        return "unknown"

    def goodies_state(self):
        """只读取浏览奖励面板，排除商品广告中的肥料、领取和购买文案。"""
        if not (self.in_app and self.package == APPS["alipay"]["package"] and self.has("浏览得奖励")):
            return None
        # 完成时整个奖励面板会消失；只接受与导航标题直接同属一个容器的终态。
        for node in self.nodes:
            if node.get("text") != "今日任务已完成":
                continue
            parent = self.parents.get(node)
            if parent is not None and any(c.get("text") == "浏览得奖励" for c in parent):
                return {"stages": [], "claimed": 0, "all_claimed": True,
                        "completed": True, "completion": "今日任务已完成"}
        if not self.has("滑动浏览或查看商品得肥料"):
            # 新版将分段栏改为单个计时横幅；金额和状态须与导航标题同属一栏。
            for title in self.nodes:
                if title.get("text") != "浏览得奖励":
                    continue
                header = self.parents.get(title)
                texts = [n.get("text", "") for n in header] if header is not None else []
                amounts = [int(s) for s in texts if s.isdecimal()]
                countdown = next((m for s in texts
                                  if (m := re.fullmatch(r"滑动浏览(\d+)秒得肥料", s))), None)
                claimed = "已获得肥料" in texts
                if len(amounts) == 1 and (countdown is not None or claimed):
                    status = "已领取" if claimed else f"{countdown[1]}s后领取"
                    return {"stages": [{"amount": amounts[0], "status": status, "claimed": claimed}],
                            "claimed": int(claimed), "all_claimed": claimed,
                            "completed": False, "completion": None, "layout": "single"}
            return None
        label = next((n for n in self.nodes
                      if re.fullmatch(r"每浏览\d+秒可得1次奖励", n.get("text", ""))
                      or n.get("text") == "已领取全部奖励"), None)
        panel = self.parents.get(label) if label is not None else None
        if panel is None:
            # 新版提示改为“当前浏览30秒可得750”；限定在导航标题旁的奖励栏，
            # 不从商品区或重复的简版顶栏收集金额和领取状态。
            for hint in self.nodes:
                if not re.fullmatch(r"当前浏览\d+秒可得\d+", hint.get("text", "")):
                    continue
                candidate = self.parents.get(hint)
                body = self.parents.get(candidate)
                if (candidate is not None and body is not None
                        and any(n.get("text") == "滑动浏览或查看商品得肥料" for n in candidate)
                        and any(n.get("text") == "浏览得奖励" for n in body)):
                    panel = candidate
                    break
        if panel is None:
            return None
        stages = []
        for child in panel:
            texts = [n.get("text", "") for n in child.iter("node")]
            amount = next((int(s) for s in texts if s.isdecimal()), None)
            # 暂停时实测显示“滑动以计时”；未知状态也必须保留，不能丢掉未领取阶段。
            status = next((s for s in texts if s == "已领取"
                           or s == "滑动以计时" or re.fullmatch(r"\d+s后领取|第\d+次", s)), None)
            if amount is not None:
                if status is None:
                    status = next((s for s in texts if s and not s.isdecimal()), "unknown")
                stages.append({"amount": amount, "status": status,
                               "claimed": status == "已领取"})
        if not stages:
            return None
        return {"stages": stages, "claimed": sum(s["claimed"] for s in stages),
                "all_claimed": all(s["claimed"] for s in stages),
                "completed": False, "completion": None}

    def goodies_cards(self):
        """只识别浏览页中成对的奖励卡片文字，保留已领取卡片供逐卡核验和去重。"""
        state = self.goodies_state()
        if state is None:
            return []
        # 新版导航横幅实际固定在顶部；若 XML 把它滚到屏外，卡片坐标也可能
        # 整体错位，即使某张卡片坐标为正也不能据此点击相邻商品。
        coordinates_valid = state.get("layout") != "single" or any(
            n.get("text") == "浏览得奖励" and self.task_tappable(n) for n in self.nodes)
        cards = []
        for label in self.nodes:
            pending = label.get("text") == "奖励现身，点我"
            claimed = label.get("text") == "已得肥料100"
            if not (pending or claimed):
                continue
            parent = self.parents.get(label)
            if parent is None:
                continue
            # 实测标签与操作文字是同一小容器的直接子节点，均可能标为不可点击。
            texts = [n.get("text", "") for n in parent]
            expected = "立即领" if pending else "已领取"
            if len([s for s in texts if s]) != 2 or expected not in texts:
                continue
            button = next(n for n in parent if n.get("text") == expected)
            path = []
            current = parent
            while current in self.parents:
                ancestor = self.parents[current]
                path.append(list(ancestor).index(current))
                current = ancestor
            # 使用结构位置而非屏幕坐标，滚动和领取文案变化不会产生新的身份。
            cards.append({"id": tuple(reversed(path)), "claimed": claimed,
                          "amount": 100, "button": button, "label": label,
                          "coordinates_valid": coordinates_valid})
        return cards

    def instant_state(self):
        """读取单次浏览任务的专属浮标，忽略商品广告中的肥料和发放文案。"""
        if not (self.in_app and self.package == APPS["alipay"]["package"]):
            return None
        headers = [(node, match) for node in self.nodes
                   if (match := re.fullmatch(r"浏览(\d+)(?:s|秒)得(\d+)肥料", node.get("text", "")))]
        if len(headers) != 1:
            return None
        header, match = headers[0]
        amount, seconds = int(match[2]), int(match[1])
        parent = self.parents.get(header)
        body = self.parents.get(parent) if parent is not None else None
        if body is None:
            return None
        candidates = []
        for child in body:
            # 实测浮标是横幅的同级容器，金额为其直接子节点，商品卡片不符合此结构。
            if not any(n.get("text") == f"{amount}肥料" for n in child):
                continue
            texts = list(dict.fromkeys(n.get("text") for n in child.iter("node") if n.get("text")))
            countdown = next((m for text in texts
                              if (m := re.fullmatch(r"滑动浏览(\d+)秒", text))), None)
            issued = "已发放" in texts
            if not issued and countdown is None:
                continue
            candidates.append({"amount": amount, "seconds": seconds, "issued": issued,
                               "remaining": int(countdown[1]) if countdown is not None else None,
                               "signals": [text for text in texts if text in (f"{amount}肥料", "已发放", "得肥料")
                                           or re.fullmatch(r"滑动浏览\d+秒", text)]})
        return candidates[0] if len(candidates) == 1 else None

    def search_terms(self):
        """仅在已验证的福利搜索入口中，从“搜索发现”选择词条，不碰历史或商品。"""
        if not (self.taobao and self.has("搜索有福利") and self.has("搜索后浏览立得奖励")):
            return []
        title = self.exact("搜索发现")
        container = self.parents.get(title) if title is not None else None
        if container is None:
            return []
        return [n for n in container.iter("node") if n.get("clickable") == "true"
                and n.get("enabled") != "false" and n.get("content-desc")
                and self.visible(n) and any(c.get("text") for c in n.iter("node"))]

    @property
    def search_results(self):
        # 固搜结果页具有独立页面标题；入口页不是可直接浏览的结果页。
        return self.taobao and self.has("芭芭农场-固搜-interact")



class ADB:
    def __init__(self, serial=None, app="taobao"):
        self.prefix = ["adb"] + (["-s", serial] if serial else [])
        self.package = APPS[app]["package"]
        self.name = APPS[app]["name"]
        self.viewport = None
        self.ui_reader = None
        self.ui_remote = None
        self.ui_buffer = b""
        self.ui_ready = False
        self.use_live_ui = False
        self.ocr_directory = None

    def run(self, *args, timeout=25):
        # 延迟解析，首次设备操作前确定路径；后续命令及持续采集器共用该路径。
        if self.prefix[0] == "adb":
            self.prefix[0] = find_adb()
        result = subprocess.run(self.prefix + list(args), capture_output=True,
                                timeout=timeout)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace").strip())
        return result.stdout.decode(errors="replace")

    def connect(self):
        if self.run("get-state").strip() != "device":
            raise RuntimeError("ADB 设备未连接或未授权")

    def running(self):
        names = self.run("shell", "ps", "-A", "-o", "NAME").splitlines()
        return any(name.strip() == self.package or name.strip().startswith(self.package + ":")
                   for name in names)

    def stop(self):
        self.run("shell", "am", "force-stop", self.package)

    def launch(self):
        output = self.run("shell", "monkey", "-p", self.package, "-c",
                          "android.intent.category.LAUNCHER", "1")
        if "Events injected: 1" not in output:
            raise RuntimeError(f"{self.name}启动失败：{output.strip()}")

    def page(self):
        # 每次使用唯一文件，dump 失败时绝不读取上次遗留 XML。
        remote = f"/sdcard/bbnc-ui-{time.time_ns()}.xml"
        try:
            if self.use_live_ui or self.ui_reader is not None:
                self.live_dump(remote)
            else:
                try:
                    result = self.run("shell", "uiautomator", "dump", "--compressed", remote)
                    if "dumped to:" not in result:
                        raise RuntimeError(f"UI 层级采集失败：{result.strip()}")
                except (RuntimeError, subprocess.TimeoutExpired):
                    # 动画列表不一定进入 idle；维持同一个无障碍连接，避免浮标节点反复丢失。
                    logging.warning("标准 UI 采集失败，切换持续无障碍采集")
                    self.live_dump(remote)
            xml = self.run("shell", "cat", remote)
            page = Page(xml, self.package)
            if page.task_list and sys.platform == "darwin":
                page.apply_task_screen(self.task_screen())
                logging.info("截图校验%s任务：%s", self.name,
                             [r["name"] for r in page.task_rows() if r["title"] in page.screen_nodes])
            self.viewport = (page.width, page.height)
            return page
        finally:
            self.run("shell", "rm", "-f", remote)

    def task_screen(self):
        """截图并调用本机 Vision；临时目录自动清理，识别失败不退回旧坐标点击。"""
        source = Path(__file__).parent / "tools" / "task-screen-ocr.m"
        if self.ocr_directory is None:
            self.ocr_directory = tempfile.TemporaryDirectory(prefix="bbnc-task-screen-")
            atexit.register(self.ocr_directory.cleanup)
        directory = Path(self.ocr_directory.name)
        image_path = directory / "screen.png"
        binary = directory / "ocr"
        if not binary.exists():
            # 同一 ADB 会话仅编译一次；临时程序不进入仓库或用户系统目录。
            subprocess.run(["clang", "-fobjc-arc", "-framework", "Foundation",
                            "-framework", "Vision", "-framework", "ImageIO",
                            "-framework", "CoreGraphics", str(source), "-o", str(binary)],
                           capture_output=True, check=True, timeout=30)
        try:
            shot = subprocess.run(self.prefix + ["exec-out", "screencap", "-p"],
                                  capture_output=True, check=True, timeout=15)
            image_path.write_bytes(shot.stdout)
            result = subprocess.run([str(binary), str(image_path)], capture_output=True,
                                    check=True, timeout=20)
            return json.loads(result.stdout)
        finally:
            image_path.unlink(missing_ok=True)

    def reader_wait(self, expected, timeout=35):
        """读取有超时的逐行协议；不对管道直接 readline，防止半行输出阻塞。"""
        deadline = time.monotonic() + timeout
        last_line = ""
        with selectors.DefaultSelector() as selector:
            selector.register(self.ui_reader.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                while b"\n" in self.ui_buffer:
                    line, self.ui_buffer = self.ui_buffer.split(b"\n", 1)
                    last_line = line.decode(errors="replace").strip()
                    if last_line == expected:
                        return
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(self.ui_reader.stdout.fileno(), 4096)
                if not chunk:
                    raise RuntimeError(f"持续 UI 采集进程已退出：{last_line[:200]}")
                self.ui_buffer += chunk
        raise RuntimeError(f"持续 UI 采集超时：{last_line[:200]}")

    def live_dump(self, remote):
        if self.ui_reader is None:
            helper = Path(__file__).parent / "tools" / "farmdump.dex"
            self.ui_remote = f"/data/local/tmp/bbnc-reader-{time.time_ns()}.dex"
            atexit.register(self.close)
            self.run("push", str(helper), self.ui_remote)
            self.run("shell", "chmod", "444", self.ui_remote)
            command = ("ANDROID_DATA=/data/local/tmp "
                       "CLASSPATH=/system/framework/android.test.runner.jar:"
                       f"/system/framework/uiautomator.jar:{self.ui_remote} "
                       "app_process /system/bin FarmDump --watch")
            self.ui_reader = subprocess.Popen(self.prefix + ["shell", command],
                                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                              stderr=subprocess.STDOUT)
        try:
            if not self.ui_ready:
                self.reader_wait("ready")
                self.ui_ready = True
            self.ui_reader.stdin.write((remote + "\n").encode())
            self.ui_reader.stdin.flush()
            self.reader_wait("dumped to: " + remote)
        except (RuntimeError, BrokenPipeError):
            self.close()
            raise

    def close(self):
        """断开只读采集器并清理上传的临时文件，不退出应用。"""
        if self.ui_reader is not None:
            reader, self.ui_reader = self.ui_reader, None
            try:
                reader.stdin.close()
                reader.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                reader.kill()
                reader.wait()
            finally:
                reader.stdout.close()
        if self.ui_remote is not None:
            remote, self.ui_remote = self.ui_remote, None
            try:
                self.run("shell", "rm", "-f", remote)
            except (RuntimeError, subprocess.TimeoutExpired):
                logging.warning("清理 UI 采集器临时文件失败：%s", remote)
        self.ui_buffer = b""
        self.ui_ready = False
        if self.ocr_directory is not None:
            self.ocr_directory.cleanup()
            self.ocr_directory = None

    def tap(self, node):
        x1, y1, x2, y2 = bounds(node)
        if x2 <= x1 or y2 <= y1:
            raise RuntimeError("节点不可见，拒绝点击")
        if self.viewport is None:
            raise RuntimeError("点击前必须先读取 UI 页面尺寸")
        x, y = self.input_point((x1 + x2) / 2, (y1 + y2) / 2, self.viewport)
        self.run("shell", "input", "tap", str(x), str(y))

    def input_point(self, x, y, viewport):
        # 部分应用有兼容缩放：支付宝实测 UI=1080×2400，触控=1260×2800。
        sizes = re.findall(r"(?:Physical|Override) size:\s*(\d+)x(\d+)",
                           self.run("shell", "wm", "size"))
        if not sizes or not all(viewport):
            raise RuntimeError("无法确定 UI 与触控坐标的比例")
        width, height = map(int, sizes[-1])
        return int(x * width / viewport[0]), int(y * height / viewport[1])

    def back(self):
        self.run("shell", "input", "keyevent", "4")


    def swipe(self, page, down=False, task_list=False):
        w, h = page.width, page.height
        if not w or not h:
            raise RuntimeError("无法读取屏幕尺寸")
        start, end = (0.4, 0.82) if down else (0.82, 0.32)
        # 实测列表中间的任务文字区域可能不响应滚动，左侧图标区域可滚动。
        x = int(w * 0.12) if task_list else w // 2
        x1, y1 = self.input_point(x, h * start, (w, h))
        x2, y2 = self.input_point(x, h * end, (w, h))
        self.run("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), "1600")


class Runner:
    def __init__(self, adb, directory, task_timeout, app="taobao"):
        self.app = app
        self.config = APPS[app]
        self.adb = adb
        self.directory = directory
        self.timeout = task_timeout
        self.results = []
        self.events = directory / "events.jsonl"
        self.scan_reports = []
        self.task_observations = {}
        self.unsupported_observations = {}
        self.skipped_tasks = []
        self.failed_tasks = []
        self.run_scope = {"mode": "unspecified", "task_keys": []}
        self._scan_handoff = None  # 仅在任务移除核验与紧接的调度之间传递覆盖证据。

    def set_run_scope(self, mode, task_keys=None):
        """声明整轮、单项、验证或只读范围；task_keys仅列本次明确选择的任务族。"""
        if mode not in ("full", "single_task", "verification", "inspect", "navigate", "scan_only"):
            raise ValueError(f"未知运行范围：{mode}")
        self.run_scope = {"mode": mode, "task_keys": list(task_keys or [])}

    def begin_task_scope(self, key, page):
        """直接调用单项处理器时自动标记范围，并保存已观察到的其他待办。"""
        if self.run_scope["mode"] == "unspecified":
            self.set_run_scope("single_task", [key])
        elif (self.run_scope["mode"] in ("single_task", "verification")
              and self.run_scope["task_keys"] and key not in self.run_scope["task_keys"]):
            raise RuntimeError(f"任务{key}不在声明的运行范围内，拒绝执行")
        self.observe_tasks(page)

    def recover_verification_failure(self, row, error):
        """隔离单项核验失败；必须重新读取并确认真实列表，不能使用处理器的旧页面继续。"""
        failure = {"task": row["name"], "key": row["key"], "before": row["current"],
                   "total": row["total"], "reason": str(error), "can_continue": False}
        self.failed_tasks.append(failure)
        try:
            page = self.perceive(retries=1)
            if not isinstance(page, Page) or not page.in_app or not page.task_list:
                raise RuntimeError("当前页面不是目标应用任务列表")
        except (RuntimeError, subprocess.TimeoutExpired, ET.ParseError) as exc:
            self.event("任务核验失败", **failure)
            raise RuntimeError(f"{error}；无法重新确认支付宝任务列表，停止后续操作：{exc}") from error
        snapshot = self.directory / f"task-verification-failed-{time.time_ns()}.xml"
        snapshot.write_bytes(ET.tostring(page.root, encoding="utf-8"))
        failure.update(can_continue=True, xml=str(snapshot))
        self.observe_tasks(page)
        self.event("任务核验失败", **failure)
        return page

    def event(self, event, **data):
        if event == "跳过任务":
            item = {k: data[k] for k in ("task", "reason", "current", "total") if k in data}
            if item not in self.skipped_tasks:
                self.skipped_tasks.append(item)
        data.setdefault("app", self.app)
        record = {"time": dt.datetime.now().astimezone().isoformat(),
                  "event": event, **data}
        with self.events.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        logging.info("%s %s", event, json.dumps(data, ensure_ascii=False))

    def perceive(self, retries=3):
        for attempt in range(retries):
            try:
                page = self.adb.page()
                if page.in_app:
                    return page
                raise RuntimeError(f"当前页面不属于{self.config['name']}")
            except (RuntimeError, subprocess.TimeoutExpired, ET.ParseError) as exc:
                self.event("感知重试", attempt=attempt + 1, reason=str(exc))
                if attempt + 1 == retries:
                    raise
                time.sleep(1)

    def perceive_active_browse(self, page):
        """慢采集期间维持支付宝计时浏览；采集线程退出后才允许点击、返回或结束。"""
        if not isinstance(self.adb, ADB) or self.app != "alipay":
            return self.perceive()
        if not page.in_app or (page.goodies_state() is None and page.instant_state() is None):
            raise RuntimeError("未确认支付宝浏览奖励页面，拒绝维持滑动")
        # 动画浏览页不能先等标准dump的idle超时，否则一次后端切换就可能耗尽采集保护时间。
        self.adb.use_live_ui = True
        started = time.monotonic()
        swipes = 0
        def finish_capture(observed):
            """给本次采集结果标注坐标新鲜性，并记录实际采集耗时与补充滑动次数。"""
            observed.browse_motion_since_capture = swipes > 0
            self.event("浏览UI采集完成", capture_seconds=round(time.monotonic() - started, 2),
                       extra_swipes=swipes)
            return observed

        # 只有采集线程使用持续UI协议；主线程仅执行前台检查与滑动，不并发读写采集管道。
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="farm-browse-read") as pool:
            future = pool.submit(self.perceive)
            while True:
                try:
                    observed = future.result(timeout=BROWSE_READ_INTERVAL)
                    return finish_capture(observed)
                except FutureTimeoutError:
                    if future.done():
                        # 采集自身抛出的TimeoutError必须传播，不能误当成仍在等待。
                        return finish_capture(future.result())
                elapsed = time.monotonic() - started
                if elapsed >= min(self.timeout, MAX_ACTIVE_READ_SECONDS):
                    raise RuntimeError("浏览UI采集过慢，停止滑动并等待采集退出，保留页面")
                activity = self.adb.run("shell", "dumpsys", "activity", "activities", timeout=5)
                resumed = ([line for line in activity.splitlines() if "topResumedActivity" in line]
                           or [line for line in activity.splitlines() if "mResumedActivity" in line])
                if not resumed or not all(re.search(r"\b" + re.escape(self.config["package"]) + r"/", line)
                                           for line in resumed):
                    raise RuntimeError("支付宝已不在前台，停止浏览滑动")
                if future.done():
                    continue
                self.adb.swipe(page)
                swipes += 1
                self.event("浏览采集中继续滑动", swipes=swipes, capture_seconds=round(elapsed, 2))

    def enter_list(self):
        page = self.perceive()
        if page.task_list:
            return page
        node = self.list_entry(page)
        if node is not None:
            self.event("进入任务列表")
            self.adb.tap(node)
            return self.wait_page(lambda p: p.task_list, "任务列表")
        raise RuntimeError("农场页面未找到任务列表入口")

    def list_entry(self, page):
        return next((node for label in self.config["list_entries"]
                     if (node := page.entry(label)) is not None), None)

    def wait_page(self, predicate, description, timeout=45):
        deadline = time.monotonic() + timeout
        misses = 0
        last_page = None
        popup_closed = False
        while time.monotonic() < deadline:
            try:
                page = self.adb.page()
                last_page = page
                if page.reward_popup:
                    close = page.reward_popup_close()
                    if close is None or popup_closed:
                        raise RuntimeError("昨日奖励弹窗关闭状态未核验，保留页面")
                    self.event("关闭昨日奖励弹窗", bounds=close.get("bounds"), claim=False)
                    self.adb.tap(close)
                    popup_closed = True
                    time.sleep(0.5)
                    continue
                if page.in_app and predicate(page):
                    self.event("页面就绪", page=description)
                    return page
                misses += 1
                if misses >= 2 and isinstance(self.adb, ADB):
                    # 部分 WebView dump 虽然成功，却只导出背景、遗漏弹窗或浮标。
                    self.adb.use_live_ui = True
            except (RuntimeError, subprocess.TimeoutExpired, ET.ParseError) as exc:
                self.event("等待页面重试", page=description, reason=str(exc))
            time.sleep(1)
        if last_page is not None:
            # 保留真正读到的页面，区分节点遗漏、未加载与实际导航变化。
            path = self.directory / f"wait-timeout-{time.time_ns()}.xml"
            path.write_bytes(ET.tostring(last_page.root, encoding="utf-8"))
            self.event("页面识别超时", page=description, xml=str(path),
                       in_app=last_page.in_app, task_list=last_page.task_list)
        raise RuntimeError(f"等待{description}超时；保留页面，不盲点其他入口")

    def bootstrap(self):
        name = self.config["name"]
        running = self.adb.running()
        self.event(f"检测{name}进程", running=running)
        if running:
            self.event(f"强制停止{name}")
            self.adb.stop()
            if self.adb.running():
                raise RuntimeError(f"{name}进程停止后仍存在，停止后续操作")
            self.event(f"{name}进程已停止")
        self.event(f"启动{name}")
        self.adb.launch()
        if isinstance(self.adb, ADB):
            # 必须在进入 WebView、打开弹窗之前建立持续连接，避免中途切换遗漏节点。
            self.adb.use_live_ui = True
        home = self.wait_page(lambda p: p.entry("芭芭农场") is not None,
                              f"{name}首页的芭芭农场入口")
        node = home.entry("芭芭农场")
        self.event("进入芭芭农场", text=node.get("text"),
                   content_desc=node.get("content-desc"), bounds=node.get("bounds"))
        self.adb.tap(node)
        farm = self.wait_page(lambda p: self.list_entry(p) is not None, "芭芭农场首页")
        node = self.list_entry(farm)
        self.event("打开任务列表", label=node.get("text") or node.get("content-desc"),
                   bounds=node.get("bounds"))
        self.adb.tap(node)
        page = self.wait_page(lambda p: p.task_list, "任务列表")
        self.event("导航完成")
        return page

    def browse(self, page):
        deadline = time.monotonic() + self.timeout
        last_page = page
        searched = False
        while time.monotonic() < deadline:
            if not page.taobao:
                raise RuntimeError("浏览中离开淘宝，停止操作")
            if page.task_list:
                raise RuntimeError("尚未进入浏览页面")
            state = page.browse_state()
            self.event("浏览状态", state=state,
                       signals=[s for s in page.texts
                                if re.search(r"滑动浏览|当前页.*下单|任务完成|立即下单.*肥料|肥料已到账|本页下单再得|奖励消失", s)])
            if state == "done":
                return
            if state == "unknown" and isinstance(self.adb, ADB):
                self.adb.use_live_ui = True
            if page.search_terms():
                if searched:
                    raise RuntimeError("搜索词点击后仍停留在入口页")
                term = page.search_terms()[0]
                self.event("点击搜索发现词条", term=term.get("content-desc"),
                           bounds=term.get("bounds"))
                self.adb.tap(term)
                searched = True
                page = self.wait_page(lambda p: p.search_results, "福利搜索结果页")
                last_page = page
                continue
            # 没有已验证词条的陌生搜索页仍只滑动，不扩大点击范围。
            self.adb.swipe(last_page)
            try:
                page = self.adb.page()
                last_page = page
            except (RuntimeError, subprocess.TimeoutExpired, ET.ParseError) as exc:
                self.event("浏览采集重试", reason=str(exc))
        raise RuntimeError("浏览超时，未读到完成文案；保留当前页面供检查")

    def return_list(self):
        # 按页面状态返回，处理搜索结果等多层页面，最多返回四次。
        for count in range(5):
            page = self.perceive()
            if page.task_list:
                return page
            entry = self.list_entry(page) if self.app == "alipay" else None
            if entry is not None:
                self.event("重新打开农场任务列表", bounds=entry.get("bounds"))
                self.adb.tap(entry)
                return self.wait_page(lambda p: p.task_list, "返回后的农场任务列表")
            if count == 4:
                break
            self.event("返回", step=count + 1)
            self.adb.back()
            time.sleep(0.5)
        raise RuntimeError("返回后未找到任务列表")

    def reveal(self, page, row):
        for down in (False, True):
            for _ in range(7):
                fresh = self.task_row(page, row)
                if fresh is None:
                    raise TaskUnavailable(f"定位期间任务未出现：{row['name']}", page)
                button = fresh["button"]
                if page.task_tappable(button) and page.task_tappable(fresh["title"]):
                    return page, fresh
                self.event("滚动定位任务", task=row["name"], direction="down" if down else "up")
                if isinstance(self.adb, ADB) and not self.adb.use_live_ui:
                    # 先建立连接再触发滚动事件；滚动后才连接可能只读到背景缓存。
                    self.adb.use_live_ui = True
                    observed = self.adb.page()
                    if not observed.in_app:
                        raise RuntimeError("滚动前已离开目标应用，停止定位")
                self.adb.swipe(page, down=down, task_list=True)
                # 一次背景快照不是“弹窗已关闭”的证据；等待期间不点击、不再滚动。
                page = self.wait_page(lambda p: p.task_list, "滚动后的任务列表", timeout=20)
        raise TaskUnavailable(f"未能定位任务入口：{row['name']}", page)


    @staticmethod
    def task_identity(row):
        """用任务族、总次数和行内奖励区分任务，允许任务标题随浏览变化。"""
        return row["key"], row["total"], row.get("reward")

    @staticmethod
    def task_row(page, row):
        # 总次数相同的真实任务也可能并存；奖励缺失时仍严格拒绝歧义。
        matches = [r for r in page.task_rows()
                   if Runner.task_identity(r) == Runner.task_identity(row)
                   and (page.screen_rows is None or page.package != PACKAGE
                        or r["title"] in page.screen_nodes)]
        if len(matches) > 1:
            raise RuntimeError("任务身份不唯一，停止匹配以免串任务")
        return matches[0] if matches else None

    def claim_gift(self, page, row):
        page, row = self.reveal(page, row)
        button = row["button"]
        before_button = button.get("text")
        if (row["current"] >= row["total"] or before_button != "去领取"
                or button.get("enabled") != "true"):
            self.event("跳过礼包", task=row["name"], progress=row["current"],
                       button=before_button, reason="已领取或未到领取时间")
            return page
        self.event("领取礼包", task=row["name"], before=row["current"],
                   button_before=before_button, bounds=button.get("bounds"))
        self.adb.tap(button)
        # 礼包在列表内直接领取，不需要滑动浏览或按返回键。
        for _ in range(3):
            page = self.perceive()
            if not page.task_list:
                raise RuntimeError("领取礼包后离开任务列表，停止操作")
            after = next((r for r in page.task_rows() if r["key"] == "gift"), None)
            after_button = after["button"].get("text") if after and after["button"] is not None else None
            if (after and after["current"] > row["current"] and after_button
                    and (after_button in ("已领取", "已完成")
                         or re.fullmatch(r"\d+点可领", after_button))):
                result = {"task": row["name"], "key": "gift", "before": row["current"],
                          "after": after["current"], "total": after["total"],
                          "button_before": before_button, "button_after": after_button,
                          "evidence": "领取次数增加且按钮变为已领取或下一领取时段",
                          "reward_messages": [s for s in page.texts if "肥料到账" in s]}
                self.results.append(result)
                self.event("任务完成", **result)
                return page
            time.sleep(1)
        raise RuntimeError(f"礼包领取未核验成功：按钮 {before_button} → {after_button}")

    def observe_tasks(self, page):
        """累积本轮见过的任务；截图未确认的旧XML不能覆盖已核验的进度。"""
        if not isinstance(page, Page) or not page.task_list or page.screen_signature == ():
            return
        rows = page.task_rows()
        names = {re.sub(r"\s+", "", r["name"]) for r in rows}
        for row in rows:
            identity = self.task_identity(row)
            confirmed = page.screen_rows is None or row["title"] in page.screen_nodes
            if not confirmed and identity in self.task_observations:
                continue
            button = row["button"].get("text") if row["button"] is not None else None
            issues = [issue["reason"] for issue in page.screen_issues
                      if issue["task"] == row["name"]]
            self.task_observations[identity] = {
                "task": row["name"], "key": row["key"], "current": row["current"],
                "total": row["total"], "reward": row.get("reward"), "button": button,
                "screenshot_confirmed": confirmed,
                "reason": issues[0] if issues else None}
        for text in page.texts:
            match = PROGRESS.fullmatch(text)
            if match is None and self.app == "alipay":
                match = re.match(r"^\s*(.+?)当前进度(\d+)，总进度(\d+)", text)
            if match is None:
                continue
            name, current, total = match.groups()
            name = name.strip()
            if re.sub(r"\s+", "", name) not in names:
                self.unsupported_observations[name, int(total)] = {
                    "task": name, "current": int(current), "total": int(total),
                    "reason": "当前执行器未支持或任务行证据不足"}

    @staticmethod
    def same_screen_signature(first, second):
        """比较非空OCR任务指纹，容忍框抖动；空指纹不构成任何静止证据。"""
        return bool(first) and bool(second) and len(first) == len(second) and all(
            a[0] == b[0] and abs(a[1] - b[1]) <= 20 for a, b in zip(first, second))

    @staticmethod
    def coverage_inventory(page):
        """仅用任务文字、进度、奖励和按钮检测列表变化，不缓存可点击坐标。"""
        return (tuple(sorted(text for text in page.texts if PROGRESS.fullmatch(text)
                             or "当前进度" in text)),
                tuple(sorted((r["key"], r["name"], r["current"], r["total"],
                              r.get("reward") or "", r["button"].get("text", "")
                              if r["button"] is not None else "") for r in page.task_rows())))

    def consume_scan_handoff(self, page):
        """只复用紧接上次移除核验的空待办覆盖；页面或任务清单变化即作废。"""
        handoff, self._scan_handoff = self._scan_handoff, None
        if (handoff is not None and isinstance(page, Page) and page.task_list
                and time.monotonic() - handoff["time"] <= 10
                and self.same_screen_signature(handoff["signature"], page.screen_signature)
                and handoff["inventory"] == self.coverage_inventory(page)):
            self.event("复用扫描覆盖", reason="刚完成移除核验，任务清单及当前位置未变化")
            return {"reuse_complete": True}
        return {}

    def finish_scan(self, page, scan, outcome, reason):
        """区分到达边界、保护上限和识别失败，并保留不完整扫描的现场。"""
        scan["outcome"] = outcome
        report = {"purpose": scan.get("purpose", "schedule"), "outcome": outcome, "reason": reason}
        if scan.get("unresolved"):
            report["unresolved_tasks"] = list(scan["unresolved"].values())
        if outcome != "complete":
            snapshot = self.directory / f"scan-incomplete-{time.time_ns()}.xml"
            snapshot.write_bytes(ET.tostring(page.root, encoding="utf-8"))
            report["xml"] = str(snapshot)
        self.scan_reports.append(report)
        self.event("扫描结束", **report)
        return None

    def scan_task_page(self, page, scan):
        """无可点击任务时滚动一屏；scan 保存方向、次数和重复页面，防止无限扫描。"""
        self.observe_tasks(page)
        if scan.pop("reuse_complete", False):
            return self.finish_scan(page, scan, "complete", "复用刚完成的完整扫描覆盖")
        if page.screen_signature is not None and not page.screen_signature:
            # 漏读时先原地重新采集，不能滑动，也不能把空结果累计为列表边界。
            scan["ocr_misses"] = scan.get("ocr_misses", 0) + 1
            scan.pop("last", None)
            scan["repeats"] = 0
            if scan["ocr_misses"] >= 3:
                return self.finish_scan(page, scan, "recognition_failed", "连续3次未识别到完整任务行")
            self.event("扫描识别重试", attempt=scan["ocr_misses"], reason="OCR任务指纹为空，原地重采集")
            time.sleep(0.5)
            return self.perceive()
        scan["ocr_misses"] = 0
        discovered = scan.setdefault("discovered", set())
        unresolved = scan.setdefault("unresolved", {})
        for row in page.task_rows():
            identity = self.task_identity(row)
            if page.screen_rows is not None:
                if row["title"] in page.screen_nodes:
                    unresolved.pop(identity, None)
                else:
                    issues = [issue for issue in page.screen_issues if issue["task"] == row["name"]]
                    if issues:
                        unresolved[identity] = {"task": row["name"], "reason": issues[0]["reason"]}
            if (row["current"] < row["total"] and row["button"] is not None
                    and row["button"].get("text") in ("去完成", "去逛逛", "去领取", "继续逛", "领取")
                    and page.task_tappable(row["title"]) and page.task_tappable(row["button"])):
                discovered.add((*self.task_identity(row), row["current"]))
        signature = tuple((n.get("text", ""), n.get("content-desc", ""), n.get("bounds", ""))
                          for n in page.nodes if page.visible(n)
                          and (n.get("text") or n.get("content-desc")))
        if page.screen_signature is not None:
            signature = page.screen_signature
        same = signature == scan.get("last")
        if page.screen_signature is not None and scan.get("last") is not None:
            same = self.same_screen_signature(signature, scan["last"])
        scan["repeats"] = scan.get("repeats", 0) + 1 if same else 0
        scan["last"] = signature
        # 先向列表下方扫描，再反向补扫，兼容从列表中间启动或任务返回后位置变化。
        if scan.get("steps", 0) >= 7 or scan["repeats"] >= 2:
            if scan["repeats"] < 2:
                scan["limited"] = True
            if scan.get("down", False):
                outcome = ("limit" if scan.get("limited") else
                           "recognition_failed" if unresolved else "complete")
                reason = ("达到滚动保护上限，覆盖未确认" if scan.get("limited") else
                          "存在未解决的截图任务身份或按钮歧义" if unresolved else
                          "两个方向均确认列表不再移动")
                return self.finish_scan(page, scan, outcome, reason)
            scan.update(down=True, steps=0, repeats=0)
            # 以当前页面为反向滚动的基线，静止次数重新计数。
        if isinstance(self.adb, ADB) and not self.adb.use_live_ui:
            self.adb.use_live_ui = True
            if not self.adb.page().in_app:
                raise RuntimeError("滚动前已离开目标应用，停止扫描")
        down = scan.get("down", False)
        self.event("滚动扫描任务", direction="down" if down else "up",
                   tasks=[{"name": r["name"], "progress": r["current"],
                           "title_tappable": page.task_tappable(r["title"]),
                           "button_tappable": page.task_tappable(r["button"])}
                          for r in page.task_rows()],
                   screenshot_verified=page.screen_rows is not None)
        self.adb.swipe(page, down=down, task_list=True)
        scan["steps"] = scan.get("steps", 0) + 1
        return self.wait_page(lambda p: p.task_list, "滚动后的任务列表", timeout=20)

    def verify_task_return(self, page, row):
        """截图模式下先找回屏外任务；双向到达边界后才允许确认任务已移除。"""
        if page.screen_rows is None:
            return page, self.task_row(page, row)
        self._scan_handoff = None
        scan = {"purpose": "verify"}
        while True:
            self.observe_tasks(page)
            after = self.task_row(page, row)
            if after is not None:
                return page, after
            next_page = self.scan_task_page(page, scan)
            if next_page is None:
                if scan.get("outcome") != "complete":
                    raise RuntimeError("截图扫描覆盖未确认，不能确认任务已移除")
                if not scan.get("discovered"):
                    self._scan_handoff = {"signature": page.screen_signature,
                                          "inventory": self.coverage_inventory(page),
                                          "time": time.monotonic()}
                self.event("截图核验任务移除", task=row["name"], evidence="双向扫描均到达列表边界")
                return page, None
            page = next_page

    def execute(self, page, only=None):
        """only 可限定一条任务身份，供当前页实机验证使用，不连带执行其他任务。"""
        if self.run_scope["mode"] != "verification":
            self.set_run_scope("single_task" if only is not None else "full",
                               [only[0]] if only is not None else None)
        if self.app == "alipay":
            return self.execute_alipay(page, only=only)
        attempted = set()
        rounds = 0
        scan = self.consume_scan_handoff(page)
        # 后续任务可能在前一任务完成后才出现，不能用首屏进度固定循环预算。
        while True:
            if not page.task_list:
                raise RuntimeError("调度时已离开淘宝任务列表，停止操作")
            self.observe_tasks(page)
            candidates = [r for r in page.task_rows()
                          if r["current"] < r["total"]
                          and (only is None or (r["key"], r["total"]) == only)
                          and (*self.task_identity(r), r["current"]) not in attempted]
            # 当前屏优先执行；屏外任务不再提前锁定，滚动后重新挑选。
            candidates = [r for r in candidates if r["button"] is not None
                          and r["button"].get("text") in ("去完成", "去逛逛", "去领取")
                          and page.task_tappable(r["title"]) and page.task_tappable(r["button"])]
            if not candidates:
                if only is not None and any((r["key"], r["total"]) == only
                                            and r["after"] >= r["total"] for r in self.results):
                    return
                next_page = self.scan_task_page(page, scan)
                if next_page is not None:
                    page = next_page
                    continue
                for gift in (r for r in page.task_rows() if r["key"] == "gift"
                             and r["current"] >= r["total"]):
                    self.event("跳过礼包", progress=gift["current"],
                               button=gift["button"].get("text") if gift["button"] is not None else None,
                               reason="本时段已领取")
                self.event("扫描结束", reason="没有可执行的已验证任务；未出现的任务不算本次完成")
                return
            # 先判断是否已全部完成，再检查上限；恰好完成最后一轮也应正常退出。
            if rounds >= MAX_TASK_ROUNDS:
                raise RuntimeError("达到执行次数上限，列表仍有待执行任务")
            rounds += 1
            scan = {}
            self._scan_handoff = None
            row = candidates[0]
            attempted.add((*self.task_identity(row), row["current"]))
            if row["key"] == "gift":
                try:
                    page = self.claim_gift(page, row)
                except TaskUnavailable as exc:
                    page = exc.page
                    self.event("跳过任务", task=row["name"], reason=str(exc))
                continue
            try:
                page, row = self.reveal(page, row)
            except TaskUnavailable as exc:
                page = exc.page
                self.event("跳过任务", task=row["name"], reason=str(exc))
                continue
            button = row["button"]
            if not page.task_tappable(button) or not page.task_tappable(row["title"]):
                raise RuntimeError(f"任务入口不可见：{row['name']}；请将列表滚到顶部")
            self.event("进入任务", task=row["name"], before=row["current"], total=row["total"],
                       bounds=button.get("bounds"))
            self.adb.tap(button)
            time.sleep(1)
            self.browse(self.perceive())
            page = self.return_list()
            page, after = self.verify_task_return(page, row)
            if after is None and row["current"] < row["total"] - 1:
                # 名称匹配和异步加载分开处理；不重复进入、不把缺失本身记为完成。
                for attempt in range(2):
                    self.event("重新核对任务", task=row["name"], total=row["total"],
                               attempt=attempt + 1)
                    time.sleep(0.5)
                    page = self.perceive()
                    if page.task_list:
                        after = self.task_row(page, row)
                        if after is not None:
                            break
            reward = [s for s in page.texts if "肥料到账" in s]
            evidence = "次数增加"
            if after is not None:
                if after["current"] <= row["current"]:
                    raise RuntimeError(f"浏览已结束但任务次数未增加：{row['name']}")
                current = after["current"]
                if after["name"] != row["name"]:
                    self.event("任务名称变化", before_name=row["name"], after_name=after["name"],
                               key=row["key"], total=row["total"], current=current)
            elif row["current"] == row["total"] - 1:
                # 最后一次可能直接移除。再次感知，排除临时未加载。
                time.sleep(1)
                page = self.perceive()
                if not page.task_list or self.task_row(page, row) is not None:
                    raise RuntimeError("任务消失状态尚未稳定，不能确认完成")
                current = row["total"]
                evidence = "浏览完成文案 + 返回后任务连续两次消失"
            else:
                snapshot = self.directory / f"task-unmatched-{time.time_ns()}.xml"
                snapshot.write_bytes(ET.tostring(page.root, encoding="utf-8"))
                self.event("任务匹配失败", task=row["name"], total=row["total"],
                           titles=[s for s in page.texts if PROGRESS.match(s)], xml=str(snapshot))
                raise RuntimeError("返回后任务身份未匹配，未确认次数；已记录当前任务标题和XML")
            result = {"task": row["name"], "key": row["key"], "before": row["current"],
                      "reward": row.get("reward"),
                      "task_after": after["name"] if after is not None else None,
                      "after": current, "total": row["total"], "evidence": evidence,
                      "reward_messages": reward}
            self.results.append(result)
            self.event("任务完成", **result)
            scan = self.consume_scan_handoff(page)

    def execute_alipay(self, page, only=None):
        """按当前列表调度，先做可点击任务；每次滚动和任务返回后重新匹配。"""
        if self.run_scope["mode"] == "unspecified":
            self.set_run_scope("single_task" if only is not None else "full",
                               [only[0]] if only is not None else None)
        handlers = {"goodies": self.execute_goodies, "featured": self.execute_featured,
                    "instant": self.execute_instant, "insurance": self.execute_insurance,
                    "saving": self.execute_saving, "home_farm": self.execute_home_farm,
                    "gold_visit": self.execute_gold_visit, "home_gold": self.execute_home_gold}
        attempted = set()
        scan = {}
        while True:
            if not page.task_list:
                raise RuntimeError("调度时已离开支付宝任务列表，停止操作")
            self.observe_tasks(page)
            if only is not None and only[0] in attempted:
                return page
            candidates = [r for r in page.task_rows()
                          if r["key"] in handlers and r["key"] not in attempted
                          and (only is None or (r["key"], r["total"]) == only)
                          and r["button"] is not None
                          and r["button"].get("enabled") != "false"
                          and r["button"].get("text") in ("去完成", "去逛逛", "继续逛", "领取")
                          and (r["current"] < r["total"]
                               # 分段首笔到账就可能变为1/1，仍检查“继续逛”中的剩余奖励。
                               or (r["key"] == "goodies"
                                   and r["button"].get("text") == "继续逛")
                               or r["button"].get("text") == "领取")]
            row = next((r for r in candidates if page.task_tappable(r["title"])
                        and page.task_tappable(r["button"])), None)
            if row is not None:
                attempted.add(row["key"])
                scan = {}
                self.event("匹配当前列表任务", task=row["name"])
                try:
                    page = handlers[row["key"]](page)
                except TaskUnavailable as exc:
                    page = exc.page
                    self.event("跳过任务", task=row["name"], reason=str(exc))
                except TaskVerificationFailed as exc:
                    # 本轮不重试该任务或再次领奖，只在新快照确认列表后继续别的任务。
                    page = self.recover_verification_failure(row, exc)
                continue
            next_page = self.scan_task_page(page, scan)
            if next_page is None:
                # 扫描结束不等于全部完成；明确记录仍未执行的任务，方便解释漏跑原因。
                pending = [r for r in page.task_rows() if r["key"] in handlers
                           and r["key"] not in attempted and r["current"] < r["total"]]
                if pending:
                    snapshot = self.directory / f"alipay-unavailable-{time.time_ns()}.xml"
                    snapshot.write_bytes(ET.tostring(page.root, encoding="utf-8"))
                    for task in pending:
                        self.event("跳过任务", task=task["name"], current=task["current"],
                                   total=task["total"], reason="双向扫描后入口仍不可定位",
                                   xml=str(snapshot))
                return page
            page = next_page

    def browse_instant(self, page, amount):
        """只滑动推进单次浏览，零秒和浮标消失均不作为发放成功的依据。"""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            state = page.instant_state()
            if state is None or state["amount"] != amount:
                raise RuntimeError("单次浏览页面未匹配或奖励金额不一致，保留页面")
            self.event("单次浏览状态", **state)
            if state["issued"]:
                return page, state
            self.adb.swipe(page)
            page = self.perceive_active_browse(page)
        raise RuntimeError("单次浏览超时，未读到奖励已发放")

    def execute_instant(self, page):
        """单次奖励与分段任务隔离；完成后不重复点击“继续逛”。"""
        self.begin_task_scope("instant", page)
        row = next((r for r in page.task_rows() if r["key"] == "instant"), None)
        if row is None:
            self.event("跳过任务", task=ALIPAY_TASKS["instant"][0], reason="任务未出现")
            return page
        button_text = row["button"].get("text") if row["button"] is not None else None
        if row["current"] == row["total"] and button_text == "领取":
            return self.claim_alipay(page, "instant", row["current"], button_text, allow_removed=True)
        if row["current"] >= row["total"] or button_text in ("已领取", "已完成"):
            self.event("跳过任务", task=row["name"], reason="单次任务此前已完成", button=button_text)
            return page
        if row["total"] != 1:
            raise TaskUnavailable("仅执行已验证的单次立得任务", page)
        page, row = self.reveal(page, row)
        if row["button"].get("text") != "去完成":
            raise TaskUnavailable("单次立得任务未找到去完成按钮", page)
        amount = int(re.search(r"立得(\d+)肥料", row["name"])[1])
        self.event("进入任务", task=row["name"], before=row["current"], total=row["total"],
                   button_before=button_text)
        self.adb.tap(row["button"])
        page = self.wait_page(lambda p: p.instant_state() is not None
                              and p.instant_state()["amount"] == amount, "单次浏览得肥料页面")
        already_issued = page.instant_state()["issued"]
        _, state = self.browse_instant(page, amount)
        page = self.return_list()
        after = self.task_row(page, row)
        # 返回动画期间列表可能先报告旧的0/1；仅重新读取，不重进任务或重复浏览。
        for attempt in range(5):
            if after is None or after["current"] == after["total"]:
                break
            self.event("等待单次任务进度刷新", task=row["name"], attempt=attempt + 1,
                       current=after["current"], total=after["total"])
            time.sleep(1)
            page = self.perceive()
            if not page.task_list:
                raise RuntimeError("等待单次任务进度刷新时离开任务列表，保留页面")
            after = self.task_row(page, row)
        details = {"displayed_amount": amount, "browse_signals": state["signals"],
                   "browse_already_issued": already_issued}
        if after is not None:
            if after["current"] != after["total"]:
                snapshot = self.directory / f"instant-progress-unmatched-{time.time_ns()}.xml"
                snapshot.write_bytes(ET.tostring(page.root, encoding="utf-8"))
                self.event("单次任务进度未完成", task=row["name"], current=after["current"],
                           total=after["total"], xml=str(snapshot))
                raise TaskVerificationFailed(f"单次浏览已发放，但返回农场后任务进度未完成；已保存XML：{snapshot}")
            after_button = after["button"].get("text") if after["button"] is not None else None
            if after_button == "领取":
                return self.claim_alipay(page, "instant", row["current"], button_text,
                                         allow_removed=True, **details)
            evidence = "专属浮标已发放，返回农场进度由0/1变为1/1"
        else:
            time.sleep(0.5)
            page = self.perceive()
            if not page.task_list or self.task_row(page, row) is not None:
                raise TaskVerificationFailed("单次立得任务移除状态未稳定")
            after_button = "任务已移除"
            evidence = "专属浮标已发放，返回后连续两次确认任务移除"
        if already_issued:
            self.event("跳过任务", task=row["name"], reason="进入浏览页时奖励已发放，不重复计入本次")
            return page
        result = {"key": "instant", "task": row["name"], "before": row["current"],
                  "after": row["total"], "total": row["total"], "button_before": button_text,
                  "button_after": after_button, **details, "evidence": evidence,
                  "reward_messages": [s for s in page.texts if "到账" in s]}
        self.results.append(result)
        self.event("任务完成", **result)
        return page

    def claim_goodies_card(self, page, card):
        """点击一张已验证卡片，仅在同一结构位置变成100肥料已领取时确认成功。"""
        self.event("领取现身卡片", card_id=card["id"], bounds=card["button"].get("bounds"))
        self.adb.tap(card["button"])
        for _ in range(3):
            page = self.perceive()
            if page.goodies_state() is None:
                raise RuntimeError("领取现身卡片后离开已验证的浏览奖励页面")
            after = next((c for c in page.goodies_cards() if c["id"] == card["id"]), None)
            if after is not None and after["claimed"]:
                reward = {"card_id": card["id"], "amount": 100,
                          "evidence": "奖励现身，点我/立即领 → 已得肥料100/已领取"}
                self.event("现身卡片奖励已核验", **reward)
                return page, reward
            time.sleep(0.3)
        raise RuntimeError("现身卡片点击后未确认100肥料已领取，不重复点击、不记为成功")

    def browse_goodies(self, page):
        """滑动推进浏览奖励，扫描并逐张核验现身卡片，不点击商品或其他活动领奖。"""
        initial = page.goodies_state()
        if initial is None:
            raise RuntimeError("未识别到逛好物分段奖励面板")
        deadline = time.monotonic() + self.timeout
        previous = initial
        new_rewards = []
        stable_done = 0
        stage_count = len(initial["stages"])
        observed_claimed = initial["claimed"]
        reward_stages = initial["stages"]
        card_ids = set()
        seen_cards = set()
        new_card_rewards = []
        scan_cards = initial.get("layout") == "single"
        card_scan_swipes = 0
        card_idle_swipes = 0

        def finish(reason):
            """分别记录本次已核验的计时奖励与卡片奖励，不推算未领取的金额。"""
            return page, {"reward_stages": reward_stages, "new_rewards": new_rewards,
                          "claimed_before": initial["claimed"], "claimed_after": observed_claimed,
                          "completion": state["completion"], "new_card_rewards": new_card_rewards,
                          "card_claims": len(new_card_rewards),
                          "card_reward_amount": sum(r["amount"] for r in new_card_rewards),
                          "cards_claimed_observed": len(card_ids), "cards_stop_reason": reason}

        while time.monotonic() < deadline:
            state = page.goodies_state()
            if state is None:
                raise RuntimeError("逛好物浏览中离开已验证的奖励页面")
            self.event("逛好物奖励状态", **state)
            cards = page.goodies_cards()
            scan_cards = scan_cards or bool(cards)
            card_ids.update(c["id"] for c in cards if c["claimed"])
            discovered = {c["id"] for c in cards} - seen_cards
            seen_cards.update(c["id"] for c in cards)
            if discovered:
                card_idle_swipes = 0
            if len(card_ids) < MAX_GOODIES_CARDS:
                card = next((c for c in cards if not c["claimed"] and c["id"] not in card_ids
                             and c["coordinates_valid"]
                             and page.task_tappable(c["button"]) and page.task_tappable(c["label"])), None)
                if card is not None:
                    if page.browse_motion_since_capture:
                        # 并发采集时坐标可能来自滚动前；先停止维持滑动并同步刷新，再决定点击。
                        self.event("刷新浏览卡片坐标", reason="采集期间发生滑动，旧坐标不可点击")
                        page = self.perceive()
                        continue
                    page, reward = self.claim_goodies_card(page, card)
                    card_ids.add(card["id"])
                    new_card_rewards.append(reward)
                    card_idle_swipes = 0
                    deadline = time.monotonic() + self.timeout
                    # 下一次循环仍与点击前的分段快照对比，不遗漏点击期间自动发放的奖励。
                    continue
            stage_count = max(stage_count, len(state["stages"]))
            observed_claimed = max(observed_claimed, state["claimed"])
            if state["stages"]:
                reward_stages = state["stages"]
            for index, stage in enumerate(state["stages"]):
                was_claimed = index < len(previous["stages"]) and previous["stages"][index]["claimed"]
                if stage["claimed"] and not was_claimed:
                    reward = {"stage": index + 1, "displayed_amount": stage["amount"],
                              "status": stage["status"]}
                    new_rewards.append(reward)
                    self.event("逛好物分段奖励已领取", **reward)
                    deadline = time.monotonic() + self.timeout
            # 两次确认所有已展示奖励均为已领取，避免阶段切换瞬间误判结束。
            if state["completed"] or (state["all_claimed"] and len(state["stages"]) == stage_count):
                stable_done = stable_done + 1 if state == previous else 1
                if stable_done >= 2:
                    stop = ("未出现现身卡片" if not scan_cards else
                            "已确认10张卡片领取" if len(card_ids) >= MAX_GOODIES_CARDS else
                            "达到卡片扫描次数上限" if card_scan_swipes >= MAX_CARD_SCAN_SWIPES else
                            "连续滑动未发现新卡片" if card_idle_swipes >= MAX_CARD_IDLE_SWIPES else None)
                    if stop is not None:
                        self.event("现身卡片扫描结束", reason=stop, confirmed=len(card_ids),
                                   new_claims=len(new_card_rewards))
                        return finish(stop)
                    self.adb.swipe(page)
                    card_scan_swipes += 1
                    card_idle_swipes += 1
                time.sleep(0.5)
            else:
                stable_done = 0
                self.adb.swipe(page)
            previous = state
            page = (self.perceive() if state["completed"] or state["all_claimed"]
                    else self.perceive_active_browse(page))
        if stable_done >= 2 and scan_cards:
            self.event("现身卡片扫描结束", reason="达到扫描时间上限", confirmed=len(card_ids),
                       new_claims=len(new_card_rewards))
            return finish("达到扫描时间上限")
        raise RuntimeError("逛好物浏览超时，奖励面板未确认全部已领取")

    def execute_featured(self, page):
        """核验精选商品的分段奖励和农场多次进度；每轮重新读取，不按入口点击次数记成功。"""
        self.begin_task_scope("featured", page)
        for attempt in range(MAX_TASK_ROUNDS):
            row = next((r for r in page.task_rows() if r["key"] == "featured"), None)
            if row is None:
                if attempt == 0:
                    self.event("跳过任务", task=ALIPAY_TASKS["featured"][0], reason="任务未出现")
                return page
            button_text = row["button"].get("text") if row["button"] is not None else None
            if row["current"] >= row["total"] or button_text in ("已领取", "已完成"):
                if row["current"] == row["total"] and button_text == "领取":
                    return self.claim_alipay(page, "featured", row["current"], button_text, allow_removed=True)
                return page
            page, row = self.reveal(page, row)
            if row["button"].get("text") != "去完成":
                raise TaskUnavailable("精选商品任务未找到去完成按钮", page)
            self.event("进入任务", task=row["name"], before=row["current"], total=row["total"],
                       button_before=button_text)
            self.adb.tap(row["button"])
            browse_page = self.wait_page(lambda p: p.goodies_state() is not None, "精选商品浏览奖励页面")
            _, details = self.browse_goodies(browse_page)
            page = self.return_list()
            after = self.task_row(page, row)
            # 返回页可能异步更新；不重新点击入口，先重读两次核对进度。
            for retry in range(2):
                if after is not None and after["current"] > row["current"]:
                    break
                if after is None and row["current"] == row["total"] - 1:
                    break
                self.event("重新核对精选商品任务", before=row["current"], attempt=retry + 1)
                time.sleep(0.5)
                page = self.perceive()
                if not page.task_list:
                    raise RuntimeError("核对精选商品进度时离开任务列表")
                after = self.task_row(page, row)
            if after is None:
                if row["current"] != row["total"] - 1:
                    snapshot = self.directory / f"featured-unmatched-{time.time_ns()}.xml"
                    snapshot.write_bytes(ET.tostring(page.root, encoding="utf-8"))
                    raise TaskVerificationFailed("精选商品任务未到最后一次却消失，未确认完成；已保存XML")
                time.sleep(0.5)
                page = self.perceive()
                if not page.task_list or self.task_row(page, row) is not None:
                    raise TaskVerificationFailed("精选商品任务移除状态未稳定")
                current, after_button = row["total"], "任务已移除"
                evidence = "分段奖励完成，返回农场后最后一次任务连续两次移除"
            else:
                current = after["current"]
                if not row["current"] < current <= row["total"]:
                    raise TaskVerificationFailed("精选商品浏览奖励已完成，但农场任务进度未增加或异常")
                after_button = after["button"].get("text") if after["button"] is not None else None
                if current == row["total"] and after_button == "领取":
                    return self.claim_alipay(page, "featured", row["current"], button_text,
                                             allow_removed=True, **details)
                evidence = "分段奖励完成，返回农场核验任务进度增加"
            result = {"key": "featured", "task": row["name"], "before": row["current"],
                      "after": current, "total": row["total"], "button_before": button_text,
                      "button_after": after_button, **details, "evidence": evidence,
                      "reward_messages": [s for s in page.texts if "到账" in s]}
            self.results.append(result)
            self.event("任务完成", **result)
            if current == row["total"]:
                return page
        raise RuntimeError("精选商品任务达到执行次数上限，仍有待执行进度")

    def execute_goodies(self, page):
        """执行分段浏览并核验农场进度，直接调用时明确标记为单项范围。"""
        self.begin_task_scope("goodies", page)
        row = next((r for r in page.task_rows() if r["key"] == "goodies"), None)
        if row is None:
            self.event("跳过任务", task=ALIPAY_TASKS["goodies"][0], reason="任务未出现")
            return page
        button_text = row["button"].get("text") if row["button"] is not None else None
        if button_text in ("已领取", "已完成"):
            self.event("跳过任务", task=row["name"], reason="已领取")
            return page
        if button_text == "领取" and row["current"] == row["total"]:
            return self.claim_alipay(page, "goodies", row["current"], button_text, allow_removed=True)
        page, row = self.reveal(page, row)
        if row["button"].get("text") not in ("去逛逛", "继续逛"):
            raise TaskUnavailable("逛好物未找到去逛逛或继续逛按钮", page)
        self.event("进入任务", task=row["name"], before=row["current"], total=row["total"])
        self.adb.tap(row["button"])
        page = self.wait_page(lambda p: p.goodies_state() is not None, "逛好物分段奖励页面")
        initial = page.goodies_state()
        page, details = self.browse_goodies(page)
        page = self.return_list()
        if initial["all_claimed"] and not details.get("new_card_rewards"):
            self.event("跳过任务", task=row["name"], reason="浏览页今日任务此前已完成或奖励已全部领取")
            return page
        after = self.task_row(page, row)
        # 分段浏览发放与农场计数可能异步更新；只重读，不再次进入或重复领取。
        for attempt in range(5):
            if after is None or after["current"] == after["total"]:
                break
            self.event("等待逛好物进度刷新", task=row["name"], attempt=attempt + 1,
                       current=after["current"], total=after["total"])
            time.sleep(0.5)
            page = self.perceive()
            if not page.task_list:
                raise RuntimeError("等待逛好物进度刷新时离开任务列表")
            self.observe_tasks(page)
            after = self.task_row(page, row)
        if after is not None and after["current"] == after["total"]:
            after_button = after["button"].get("text") if after["button"] is not None else None
            if after_button == "领取":
                return self.claim_alipay(page, "goodies", row["current"], button_text,
                                         allow_removed=True, **details)
        elif after is None:
            time.sleep(0.5)
            page = self.perceive()
            if not page.task_list or self.task_row(page, row) is not None:
                raise TaskVerificationFailed("逛好物返回后任务移除状态未稳定")
        else:
            snapshot = self.directory / f"goodies-progress-unmatched-{time.time_ns()}.xml"
            snapshot.write_bytes(ET.tostring(page.root, encoding="utf-8"))
            self.event("逛好物进度未完成", task=row["name"], current=after["current"],
                       total=after["total"], xml=str(snapshot))
            raise TaskVerificationFailed(f"逛好物奖励已领取，但农场任务进度未完成；已保存XML：{snapshot}")
        if (row["current"] >= row["total"] and not details["new_rewards"]
                and not details.get("new_card_rewards")):
            self.event("跳过任务", task=row["name"], reason="农场此前已完成，未观察到本次新增分段奖励")
            return page
        result = {"key": "goodies", "task": row["name"], "before": row["current"],
                  "after": row["total"], "total": row["total"], **details,
                  "evidence": ("浏览页连续两次确认今日任务已完成，返回农场核验完成"
                               if details.get("completion") else
                               "分段奖励连续两次全部已领取，返回农场核验任务完成或连续两次移除"),
                  "reward_messages": [s for s in page.texts if "到账" in s]}
        self.results.append(result)
        self.event("任务完成", **result)
        return page

    def saving_observe(self):
        # 只处理实测过的“支付宝想打开淘宝”系统弹窗，不授权跨应用打开。
        page = self.adb.page()
        cancel = page.cancel_taobao_prompt()
        if cancel is not None:
            self.event("取消跨应用跳转", destination="淘宝")
            self.adb.tap(cancel)
            return self.perceive()
        if not page.in_app:
            raise RuntimeError("省钱会场出现未知外部页面，停止操作")
        return page

    def saving_update(self, previous):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            page = self.saving_observe()
            progress = page.saving_progress()
            if page.saving_done or (progress and progress[0] > previous):
                self.event("商品点击已核验", before=previous,
                           after=progress[0] if progress else None,
                           completion=page.saving_done)
                return page
            if page.saving_destination:
                self.event("返回省钱会场")
                self.adb.back()
            elif not page.saving_venue:
                raise RuntimeError("商品点击后出现未识别的页面")
            # 正在跳转详情页时继续感知，不提前按返回或再次点击。
            time.sleep(1)
        raise RuntimeError("商品点击后会场进度未增加，停止；不将点击动作记为成功")

    def saving_return_list(self):
        for _ in range(7):
            page = self.saving_observe()
            if page.task_list:
                return page
            if page.saving_venue or page.saving_destination:
                if page.has("正在跳转详情页"):
                    time.sleep(1)
                    continue
                self.event("返回农场任务列表")
                self.adb.back()
            elif self.list_entry(page) is not None:
                self.adb.tap(self.list_entry(page))
            else:
                raise RuntimeError("省钱会场返回路径未识别")
            time.sleep(1)
        raise RuntimeError("省钱会场返回任务列表超时")

    def execute_saving(self, page):
        """执行省钱会场并通过公共领奖核验记录结果。"""
        self.begin_task_scope("saving", page)
        row = next((r for r in page.task_rows() if r["key"] == "saving"), None)
        if row is None:
            self.event("跳过任务", task="逛一逛省钱会场", reason="任务未出现")
            return page
        button_text = row["button"].get("text") if row["button"] is not None else None
        if button_text in ("已领取", "已完成"):
            self.event("跳过任务", task=row["name"], button=button_text, reason="已领取")
            return page
        original = row["current"]
        product_clicks = []
        if row["current"] < row["total"]:
            page, row = self.reveal(page, row)
            if row["button"] is None or row["button"].get("text") != "去完成":
                raise RuntimeError("省钱会场未找到去完成按钮")
            self.event("进入任务", task=row["name"], before=original,
                       button_before=row["button"].get("text"))
            self.adb.tap(row["button"])
            page = self.wait_page(lambda p: p.saving_venue and (
                p.saving_progress() is not None or p.saving_done), "省钱会场")
            seen = set()
            for _ in range(3):
                if page.saving_done:
                    break
                progress = page.saving_progress()
                if progress is None or progress[1] != 3:
                    raise RuntimeError("会场未显示点3个商品得1000肥料，拒绝继续点击")
                products = [n for n in page.saving_products()
                            if n.get("content-desc") not in seen]
                if not products:
                    raise RuntimeError("屏幕内没有未点击的奖励商品，保留页面供检查")
                product = products[0]
                label = product.get("content-desc")
                seen.add(label)
                self.event("点击奖励商品", product=label, before=progress[0],
                           bounds=product.get("bounds"))
                self.adb.tap(product)
                page = self.saving_update(progress[0])
                after = page.saving_progress()
                product_clicks.append({"product": label, "before": progress[0],
                                       "after": after[0] if after else None})
            if not page.saving_done:
                raise RuntimeError("未确认省钱会场3/3完成")
            self.event("会场浏览完成", signals=[s for s in page.texts
                       if SAVING_PROGRESS.search(s) or "恭喜！完成任务" in s
                       or "再点3个商品" in s])
            page = self.saving_return_list()
        return self.claim_alipay(page, "saving", original, button_text,
                                 product_clicks=product_clicks)

    def execute_home_farm(self, page):
        """从任务入口打开首页服务引导，再访问农场并核验领奖。"""
        return self.execute_alipay_visit(page, "home_farm")

    def execute_gold_visit(self, page):
        """只访问金豆会场并返回，不点击待领取金豆、兑换或抽奖入口。"""
        return self.execute_alipay_visit(page, "gold_visit")

    def execute_home_gold(self, page):
        """经引导返回支付宝首页，通过常用入口访问金豆，再回农场领奖。"""
        return self.execute_alipay_visit(page, "home_gold")

    def open_common_app(self, page, label):
        """仅经原生首页常用区或其全部列表进入应用，缺失时保留可返回的现场。"""
        node = page.common_app(label)
        if node is None and page.alipay_home:
            all_apps = page.entry("全部")
            if all_apps is not None:
                self.adb.tap(all_apps)
                page = self.wait_page(lambda p: p.has("最近常用应用"), "首页最近常用应用")
                node = page.common_app(label)
        if node is None:
            raise TaskUnavailable(f"首页常用区未找到{label}，需先在支付宝配置该入口", page)
        self.event("从首页常用访问应用", target=label, bounds=node.get("bounds"))
        self.adb.tap(node)

    def return_alipay_farm(self):
        """金豆可返回农场或首页；只沿这两条实测路径找回农场任务列表。"""
        for _ in range(6):
            page = self.perceive()
            if page.task_list:
                return page
            if self.list_entry(page) is not None:
                return self.enter_list()
            if page.alipay_home or page.has("最近常用应用"):
                self.open_common_app(page, "芭芭农场")
                self.wait_page(lambda p: self.list_entry(p) is not None, "返回的农场首页")
                return self.enter_list()
            if page.gold_venue:
                self.adb.back()
                time.sleep(1)
                continue
            raise RuntimeError("访问任务返回路径未识别，保留页面")
        raise RuntimeError("访问任务返回农场超时")

    def execute_alipay_visit(self, page, key):
        """访问类任务独立记账；必须回到农场确认1/1，再通过公共流程核验领取。"""
        self.begin_task_scope(key, page)
        row = next((r for r in page.task_rows() if r["key"] == key), None)
        if row is None:
            self.event("跳过任务", task=ALIPAY_TASKS[key][0], reason="任务未出现")
            return page
        if row["button"] is not None and row["button"].get("text") in ("已领取", "已完成"):
            self.event("跳过任务", task=row["name"], reason="此前已完成")
            return page
        original = row["current"]
        page, row = self.reveal(page, row)
        button_text = row["button"].get("text")
        if row["total"] != 1:
            raise TaskUnavailable("访问任务总次数不是已验证的1次", page)
        if row["current"] < row["total"]:
            if button_text != "去完成":
                raise TaskUnavailable("访问任务没有可执行的去完成入口", page)
            self.event("进入任务", task=row["name"], before=original, button_before=button_text)
            self.adb.tap(row["button"])
            if key == "home_farm":
                destination = self.wait_page(lambda p: p.farm_home_service or p.alipay_home,
                                             "首页访问农场引导")
                if destination.farm_home_service:
                    self.adb.tap(destination.entry("芭芭农场"))
                else:
                    self.open_common_app(destination, "芭芭农场")
                self.wait_page(lambda p: self.list_entry(p) is not None, "首页访问后的农场")
            elif key == "home_gold":
                destination = self.wait_page(lambda p: p.entry("从常用访问金豆") is not None
                                             or p.alipay_home, "首页访问金豆引导")
                if not destination.alipay_home:
                    self.adb.tap(destination.entry("从常用访问金豆"))
                    destination = self.wait_page(lambda p: p.alipay_home, "支付宝原生首页")
                try:
                    self.open_common_app(destination, "金豆夺宝")
                except TaskUnavailable as exc:
                    # 缺少首页入口是可跳过任务，先回农场再交回调度器，不能留在外页继续做任务。
                    page = self.return_alipay_farm()
                    raise TaskUnavailable(str(exc), page) from exc
                self.wait_page(lambda p: p.gold_venue, "首页访问金豆会场")
            else:
                self.wait_page(lambda p: p.gold_venue, "金豆夺宝会场")
            page = self.return_alipay_farm()
            # 用实时位置找回原任务，避免屏外旧进度阻止领奖，也不凭停留时间认定完成。
            fresh = self.task_row(page, row)
            if fresh is None:
                raise TaskVerificationFailed(f"返回后未找到{row['name']}，不能确认访问完成")
            page, fresh = self.reveal(page, fresh)
            for attempt in range(5):
                if fresh["current"] == fresh["total"]:
                    break
                self.event("等待访问任务进度", task=row["name"], attempt=attempt + 1)
                time.sleep(1)
                page = self.perceive()
                if not page.task_list:
                    raise RuntimeError("等待访问任务进度时离开任务列表")
                fresh = self.task_row(page, row)
                if fresh is None:
                    raise TaskVerificationFailed("访问任务暂时缺失，不能确认完成")
        return self.claim_alipay(page, key, original, button_text, allow_removed=True,
                                 visit_route=key, visit_verified=original < row["total"])

    def execute_insurance(self, page):
        """浏览车险搜索页，回农场后核验进度及领奖。"""
        self.begin_task_scope("insurance", page)
        row = next((r for r in page.task_rows() if r["key"] == "insurance"), None)
        if row is None:
            self.event("跳过任务", task="搜一搜「车险」", reason="任务未出现")
            return page
        button_text = row["button"].get("text") if row["button"] is not None else None
        if button_text in ("已领取", "已完成"):
            self.event("跳过任务", task=row["name"], button=button_text, reason="已领取")
            return page
        original = row["current"]
        signals = []
        if row["current"] < row["total"]:
            page, row = self.reveal(page, row)
            if row["button"] is None or row["button"].get("text") != "去完成":
                raise RuntimeError("车险任务未找到去完成按钮")
            self.event("进入任务", task=row["name"], before=original, button_before="去完成")
            self.adb.tap(row["button"])
            page = self.wait_page(lambda p: p.insurance_search, "车险搜索结果页")
            deadline = time.monotonic() + self.timeout
            while True:
                if not page.insurance_search:
                    raise RuntimeError("车险浏览过程中离开已识别的搜索页面")
                signals = [s for s in page.texts if s in ("浏览3秒", "得肥料", "肥料奖励", "已发放")]
                self.event("车险浏览状态", task=row["name"], signals=signals,
                           completed=page.insurance_done)
                if page.insurance_done:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("车险浏览超时，未读到肥料奖励已发放")
                # 该任务实测停留即可完成，无需滚动或点击保险产品。
                time.sleep(1)
                page = self.perceive()
            self.event("返回农场任务列表", task=row["name"])
            self.adb.back()
            page = self.wait_page(lambda p: p.task_list, "车险任务返回后的农场列表")
        return self.claim_alipay(page, "insurance", original, button_text,
                                 allow_removed=True, browse_signals=signals)

    def claim_alipay(self, page, key, original, button_text, allow_removed=False, **details):
        """只在确认的列表中领取；进度或领奖无法核验时交给调度器隔离失败。"""
        if not page.task_list:
            raise RuntimeError("领奖前未确认支付宝任务列表，停止操作")
        name = ALIPAY_TASKS[key][0]
        row = next((r for r in page.task_rows() if r["key"] == key), None)
        if row is None or row["current"] != row["total"]:
            raise TaskVerificationFailed(f"返回农场后{name}任务进度未完成")
        page, row = self.reveal(page, row)
        button = row["button"]
        if button is None or button.get("text") != "领取":
            raise TaskVerificationFailed(f"{name}已完成但未找到领取按钮，不记为已领取")
        self.event("领取任务奖励", task=row["name"], button_before="领取")
        self.adb.tap(button)
        missing = 0
        for _ in range(3):
            page = self.perceive()
            if not page.task_list:
                raise RuntimeError(f"领取{name}奖励后离开任务列表")
            after = next((r for r in page.task_rows() if r["key"] == key), None)
            missing = missing + 1 if after is None else 0
            claimed = (after and after["button"] is not None
                    and after["current"] == after["total"]
                    and after["button"].get("text") == "已领取")
            removed = allow_removed and missing >= 2
            if claimed or removed:
                result = {"key": key, "task": row["name"], "before": original,
                          "after": row["total"], "total": row["total"],
                          "button_before": button_text,
                          "button_after": "任务已移除" if removed else "已领取",
                          "claim_button_before": "领取", **details,
                          "evidence": ("农场进度已完成，点击领取后连续两次确认任务移除" if removed
                                       else "农场进度完成且奖励按钮由领取变为已领取"),
                          "reward_messages": [s for s in page.texts if "到账" in s]}
                self.results.append(result)
                self.event("任务完成", **result)
                return page
            time.sleep(1)
        raise TaskVerificationFailed(f"{name}奖励领取状态未确认")

    def summary(self, error=None):
        # 结果是本轮已核验动作的最终依据，旧快照不能把已完成任务重新列为待办。
        observations = {identity: dict(item) for identity, item in self.task_observations.items()}
        for result in self.results:
            for identity, item in observations.items():
                if (item["key"] == result["key"] and item["total"] == result.get("total")
                        and (self.app == "alipay" or item.get("reward") == result.get("reward"))):
                    item["current"] = max(item["current"], result["after"])
                    item["button"] = result.get("button_after")
        pending = [item for item in observations.values()
                   if item["current"] < item["total"] or item["button"] == "领取"]
        incomplete = any(report["outcome"] != "complete" for report in self.scan_reports)
        coverage = ("incomplete" if incomplete else
                    "complete" if any(r["purpose"] == "schedule" for r in self.scan_reports) else
                    "task_verification_only" if self.scan_reports else "not_scanned")
        mode = self.run_scope["mode"]
        scoped_pending = [item for item in pending if item["key"] in self.run_scope["task_keys"]]
        full_complete = (mode == "full" and coverage == "complete"
                         and not error and not incomplete and not pending and not self.failed_tasks)
        if error:
            status = "error"
        elif incomplete or self.failed_tasks:
            status = "partial"
        elif mode == "verification":
            status = "verification_ok"
        elif mode == "single_task":
            status = "partial" if scoped_pending else "single_task_ok"
        elif mode in ("inspect", "navigate", "scan_only"):
            status = {"inspect": "inspected", "navigate": "navigated", "scan_only": "scanned"}[mode]
        else:
            status = "partial" if pending or (mode == "full" and not full_complete) else "ok"
        summary = {"app": self.app, "status": status, "error": error,
                   "completed_rounds": len(self.results),
                   "browse_rounds": sum(r["key"] != "gift" for r in self.results),
                   "gift_claims": sum(r["key"] == "gift" for r in self.results),
                   "results": self.results,
                   "run_scope": dict(self.run_scope),
                   "all_supported_tasks_complete": full_complete,
                   "failed_tasks": self.failed_tasks,
                   "scan_coverage": {"status": coverage, "reports": self.scan_reports},
                   "pending_tasks": pending, "skipped_tasks": self.skipped_tasks,
                   "unsupported_tasks": list(self.unsupported_observations.values())}
        (self.directory / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        self.event("本次汇总", **summary)
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", choices=APPS, default="taobao", help="选择应用，默认 taobao")
    parser.add_argument("--serial", help="ADB 设备序列号；多设备时必填")
    parser.add_argument("--inspect", action="store_true", help="只读取页面，不点击或滑动")
    parser.add_argument("--navigate-only", action="store_true", help="执行启动和导航，停在任务列表")
    parser.add_argument("--from-current", action="store_true", help="复用当前任务列表，不重启或导航")
    parser.add_argument("--task-timeout", type=int, default=90, help="单次浏览超时秒数")
    parser.add_argument("--logs", type=Path, default=Path("logs"))
    args = parser.parse_args()
    if args.inspect and args.navigate_only:
        parser.error("--inspect 与 --navigate-only 不能同时使用")
    if args.from_current and (args.inspect or args.navigate_only):
        parser.error("--from-current 只用于当前列表执行任务")
    directory = args.logs / (dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + args.app)
    directory.mkdir(parents=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(directory / "run.log", encoding="utf-8")])
    runner = Runner(ADB(args.serial, args.app), directory, args.task_timeout, args.app)
    runner.set_run_scope("inspect" if args.inspect else "navigate" if args.navigate_only else "full")
    error = None
    try:
        runner.adb.connect()
        runner.event("开始", mode="inspect" if args.inspect else (
            "navigate" if args.navigate_only else "run"), logs=str(directory))
        if args.inspect:
            page = runner.perceive()
            runner.observe_tasks(page)
            (directory / "page.xml").write_bytes(ET.tostring(page.root, encoding="utf-8"))
            runner.event("页面检查", task_list=page.task_list, browse_state=page.browse_state(),
                         tasks=[{**{k: v for k, v in r.items() if k not in ("title", "button")},
                                 "button_text": r["button"].get("text") if r["button"] is not None else None}
                                for r in page.task_rows()])
        else:
            page = (runner.wait_page(lambda p: p.task_list, "当前任务列表")
                    if args.from_current else runner.bootstrap())
            if args.from_current and not page.task_list:
                raise RuntimeError("当前页面不是任务列表，保留页面")
            if not args.navigate_only:
                runner.event("开始做任务")
                runner.execute(page)
    except KeyboardInterrupt:
        error = "用户中止，未核验的任务不计入完成"
    except Exception as exc:
        error = str(exc)
        logging.error("停止：%s", error)
    finally:
        runner.adb.close()
        runner.summary(error)
        logging.info("日志目录：%s", directory.resolve())
    return 1 if error else 0


if __name__ == "__main__":
    try:
        ensure_xml_runtime()
    except (RuntimeError, OSError) as exc:
        print(f"启动检查失败：{exc}", file=sys.stderr)
        sys.exit(1)
    sys.exit(main())
