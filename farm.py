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
import xml.etree.ElementTree as ET


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
}
PROGRESS = re.compile(r"^(.+?)[(（](\d+)/(\d+)[)）]$")
SAVING_TASK = re.compile(r"^\s*逛一逛省钱会场当前进度(\d+)，总进度(\d+)")
ALIPAY_TASKS = {
    "instant": ("逛好物立得肥料", re.compile(
        r"^\s*(?:逛好物|浏览好物)立得\d+肥料当前进度(\d+)，总进度(\d+)")),
    "goodies": ("逛好物最高得肥料", re.compile(
        r"^\s*逛好物最高得\d+肥料当前进度(\d+)，总进度(\d+)")),
    "saving": ("逛一逛省钱会场", SAVING_TASK),
    "insurance": ("搜一搜「车险」", re.compile(
        r"^\s*搜一搜[「“\"]车险[」”\"]当前进度(\d+)，总进度(\d+)")),
}
SAVING_PROGRESS = re.compile(r"点3个商品，得1000肥料[（(](\d+)/(\d+)[）)]")
MAX_TASK_ROUNDS = 100  # 只限制任务尝试次数，不限制完成后的最终列表检查。


class TaskUnavailable(RuntimeError):
    """任务未出现或不可定位；携带最后确认的列表，供调度器继续其他任务。"""
    def __init__(self, message, page):
        super().__init__(message)
        self.page = page


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
            return self.in_app and self.has("做任务集肥料") and self.has("关闭做任务集肥料弹窗")
        return self.has("肥料明细") and any(
            n.get("text") in ("去完成", "去领取", "去开启", "已领取", "已完成")
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

    def entry(self, label):
        return next((n for n in self.nodes
                     if n.get("package") == self.package
                     and (n.get("text") == label or n.get("content-desc") == label)
                     and n.get("enabled") != "false" and self.visible(n)), None)

    def task_rows(self):
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
                               if (n.get("text") in ("去完成", "已完成", "去领取", "已领取")
                                   or re.fullmatch(r"\d+点可领", n.get("text", "")))
                               and n.get("clickable") == "true"), None)
                if button is not None:
                    break
                parent = self.parents.get(parent)
            rows.append({"key": key, "name": name, "current": int(current),
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
            return None
        label = next((n for n in self.nodes
                      if re.fullmatch(r"每浏览\d+秒可得1次奖励", n.get("text", ""))), None)
        panel = self.parents.get(label) if label is not None else None
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
            self.viewport = (page.width, page.height)
            return page
        finally:
            self.run("shell", "rm", "-f", remote)

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

    def event(self, event, **data):
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
                                if re.search(r"滑动浏览|当前页.*下单|任务完成|立即下单.*肥料", s)])
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
                if button is not None and page.visible(button) and page.visible(fresh["title"]):
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
    def task_row(page, row):
        # 同一列表可能同时有 18 次、5 次的同名推荐任务，以总次数区分身份。
        matches = [r for r in page.task_rows()
                   if (r["key"], r["total"]) == (row["key"], row["total"])]
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

    def execute(self, page, only=None):
        """only 可限定一条任务身份，供当前页实机验证使用，不连带执行其他任务。"""
        if self.app == "alipay":
            for key, handler in (("goodies", self.execute_goodies),
                                 ("instant", self.execute_instant),
                                 ("insurance", self.execute_insurance), ("saving", self.execute_saving)):
                try:
                    page = handler(page)
                except TaskUnavailable as exc:
                    page = exc.page
                    self.event("跳过任务", task=ALIPAY_TASKS[key][0], reason=str(exc))
            return page
        attempted = set()
        rounds = 0
        # 后续任务可能在前一任务完成后才出现，不能用首屏进度固定循环预算。
        while True:
            candidates = [r for r in page.task_rows()
                          if r["current"] < r["total"]
                          and (only is None or (r["key"], r["total"]) == only)
                          and (r["key"], r["total"], r["current"]) not in attempted]
            if not candidates:
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
            row = candidates[0]
            attempted.add((row["key"], row["total"], row["current"]))
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
            if button is None or not page.visible(button) or not page.visible(row["title"]):
                raise RuntimeError(f"任务入口不可见：{row['name']}；请将列表滚到顶部")
            self.event("进入任务", task=row["name"], before=row["current"], total=row["total"],
                       bounds=button.get("bounds"))
            self.adb.tap(button)
            time.sleep(1)
            self.browse(self.perceive())
            page = self.return_list()
            after = self.task_row(page, row)
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
                      "task_after": after["name"] if after is not None else None,
                      "after": current, "total": row["total"], "evidence": evidence,
                      "reward_messages": reward}
            self.results.append(result)
            self.event("任务完成", **result)

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
            page = self.perceive()
        raise RuntimeError("单次浏览超时，未读到奖励已发放")

    def execute_instant(self, page):
        """单次奖励与分段任务隔离；完成后不重复点击“继续逛”。"""
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
        details = {"displayed_amount": amount, "browse_signals": state["signals"],
                   "browse_already_issued": already_issued}
        if after is not None:
            if after["current"] != after["total"]:
                raise RuntimeError("单次浏览已发放，但返回农场后任务进度未完成")
            after_button = after["button"].get("text") if after["button"] is not None else None
            if after_button == "领取":
                return self.claim_alipay(page, "instant", row["current"], button_text,
                                         allow_removed=True, **details)
            evidence = "专属浮标已发放，返回农场进度由0/1变为1/1"
        else:
            time.sleep(0.5)
            page = self.perceive()
            if not page.task_list or self.task_row(page, row) is not None:
                raise RuntimeError("单次立得任务移除状态未稳定")
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

    def browse_goodies(self, page):
        """滑动推进分段奖励；只接受面板“已领取”，不点商品或其他活动领奖。"""
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
        while time.monotonic() < deadline:
            state = page.goodies_state()
            if state is None:
                raise RuntimeError("逛好物浏览中离开已验证的奖励页面")
            self.event("逛好物奖励状态", **state)
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
                    return page, {"reward_stages": reward_stages, "new_rewards": new_rewards,
                                  "claimed_before": initial["claimed"], "claimed_after": observed_claimed,
                                  "completion": state["completion"]}
                time.sleep(0.5)
            else:
                stable_done = 0
                self.adb.swipe(page)
            previous = state
            page = self.perceive()
        raise RuntimeError("逛好物浏览超时，奖励面板未确认全部已领取")

    def execute_goodies(self, page):
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
        if initial["all_claimed"]:
            self.event("跳过任务", task=row["name"], reason="浏览页今日任务此前已完成或奖励已全部领取")
            return page
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
                raise RuntimeError("逛好物返回后任务移除状态未稳定")
        else:
            raise RuntimeError("逛好物奖励已领取，但农场任务进度未完成")
        if row["current"] >= row["total"] and not details["new_rewards"]:
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

    def execute_insurance(self, page):
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
        name = ALIPAY_TASKS[key][0]
        row = next((r for r in page.task_rows() if r["key"] == key), None)
        if row is None or row["current"] != row["total"]:
            raise RuntimeError(f"返回农场后{name}任务进度未完成")
        page, row = self.reveal(page, row)
        button = row["button"]
        if button is None or button.get("text") != "领取":
            raise RuntimeError(f"{name}已完成但未找到领取按钮，不记为已领取")
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
        raise RuntimeError(f"{name}奖励领取状态未确认")

    def summary(self, error=None):
        summary = {"app": self.app, "status": "error" if error else "ok", "error": error,
                   "completed_rounds": len(self.results),
                   "browse_rounds": sum(r["key"] != "gift" for r in self.results),
                   "gift_claims": sum(r["key"] == "gift" for r in self.results),
                   "results": self.results}
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
    error = None
    try:
        runner.adb.connect()
        runner.event("开始", mode="inspect" if args.inspect else (
            "navigate" if args.navigate_only else "run"), logs=str(directory))
        if args.inspect:
            page = runner.perceive()
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
