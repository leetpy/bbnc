#!/usr/bin/env python3
"""通过 ADB 进入淘宝/支付宝芭芭农场，执行已验证的淘宝任务。"""

import argparse
import datetime as dt
import json
import logging
from pathlib import Path
import re
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
    "selected": ("看严选推荐商品", "发现精选好物"),
    "browse15": ("逛精选好物",),
    "gift": ("领肥料礼包",),
}
PROGRESS = re.compile(r"^(.+?)[(（](\d+)/(\d+)[)）]$")
SAVING_TASK = re.compile(r"^\s*逛一逛省钱会场当前进度(\d+)，总进度(\d+)")
ALIPAY_TASKS = {
    "saving": ("逛一逛省钱会场", SAVING_TASK),
    "insurance": ("搜一搜「车险」", re.compile(
        r"^\s*搜一搜[「“\"]车险[」”\"]当前进度(\d+)，总进度(\d+)")),
}
SAVING_PROGRESS = re.compile(r"点3个商品，得1000肥料[（(](\d+)/(\d+)[）)]")


def bounds(node):
    values = list(map(int, re.findall(r"-?\d+", node.get("bounds", ""))))
    return tuple(values) if len(values) == 4 else (0, 0, 0, 0)


class Page:
    def __init__(self, xml, package=PACKAGE):
        self.package = package
        self.root = ET.fromstring(xml)
        self.nodes = list(self.root.iter("node"))
        self.parents = {child: parent for parent in self.root.iter() for child in parent}
        self.texts = [n.get("text", "") for n in self.nodes]
        self.width = max((bounds(n)[2] for n in self.nodes), default=0)
        self.height = max((bounds(n)[3] for n in self.nodes), default=0)

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
        if self.package == APPS["alipay"]["package"]:
            return self.in_app and self.has("做任务集肥料") and self.has("关闭做任务集肥料弹窗")
        return self.has("肥料明细") and any(
            n.get("text") in ("去完成", "去领取", "去开启", "已领取", "已完成")
            or re.fullmatch(r"\d+点可领", n.get("text", "")) for n in self.nodes
        )

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
                parent = self.parents.get(node)
                button = next((n for n in parent.iter("node")
                               if n.get("text") in ("去完成", "领取", "已领取", "已完成")
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


class ADB:
    def __init__(self, serial=None, app="taobao"):
        self.prefix = ["adb"] + (["-s", serial] if serial else [])
        self.package = APPS[app]["package"]
        self.name = APPS[app]["name"]
        self.viewport = None

    def run(self, *args, timeout=25):
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
            result = self.run("shell", "uiautomator", "dump", "--compressed", remote)
            if "dumped to:" not in result:
                raise RuntimeError(f"UI 层级采集失败：{result.strip()}")
            xml = self.run("shell", "cat", remote)
            page = Page(xml, self.package)
            self.viewport = (page.width, page.height)
            return page
        finally:
            self.run("shell", "rm", "-f", remote)

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
        while time.monotonic() < deadline:
            try:
                page = self.adb.page()
                if page.in_app and predicate(page):
                    self.event("页面就绪", page=description)
                    return page
            except (RuntimeError, subprocess.TimeoutExpired, ET.ParseError) as exc:
                self.event("等待页面重试", page=description, reason=str(exc))
            time.sleep(1)
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
            # 搜索入口也先滑动，不点击词条。未知页面有超时限制。
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
            if count == 4:
                break
            self.event("返回", step=count + 1)
            self.adb.back()
            time.sleep(0.5)
        raise RuntimeError("返回后未找到任务列表")

    def reveal(self, page, row):
        for down in (False, True):
            for _ in range(7):
                fresh = next((r for r in page.task_rows() if r["key"] == row["key"]), None)
                if fresh is None:
                    raise RuntimeError(f"定位期间任务消失：{row['name']}")
                button = fresh["button"]
                if button is not None and page.visible(button) and page.visible(fresh["title"]):
                    return page, fresh
                self.event("滚动定位任务", task=row["name"], direction="down" if down else "up")
                self.adb.swipe(page, down=down, task_list=True)
                page = self.perceive()
                if not page.task_list:
                    raise RuntimeError("滚动后离开任务列表")
        raise RuntimeError(f"未能滚动到任务入口：{row['name']}")

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

    def execute(self, page):
        if self.app == "alipay":
            page = self.execute_insurance(page)
            return self.execute_saving(page)
        attempted = set()
        for _ in range(12):
            candidates = [r for r in page.task_rows()
                          if r["current"] < r["total"]
                          and (r["key"], r["current"]) not in attempted]
            if not candidates:
                for gift in (r for r in page.task_rows() if r["key"] == "gift"
                             and r["current"] >= r["total"]):
                    self.event("跳过礼包", progress=gift["current"],
                               button=gift["button"].get("text") if gift["button"] is not None else None,
                               reason="本时段已领取")
                self.event("扫描结束", reason="没有可执行的已验证任务；未出现的任务不算本次完成")
                return
            row = candidates[0]
            attempted.add((row["key"], row["current"]))
            if row["key"] == "gift":
                page = self.claim_gift(page, row)
                continue
            page, row = self.reveal(page, row)
            button = row["button"]
            if button is None or not page.visible(button) or not page.visible(row["title"]):
                raise RuntimeError(f"任务入口不可见：{row['name']}；请将列表滚到顶部")
            self.event("进入任务", task=row["name"], before=row["current"], total=row["total"],
                       bounds=button.get("bounds"))
            self.adb.tap(button)
            time.sleep(1)
            self.browse(self.perceive())
            page = self.return_list()
            after = next((r for r in page.task_rows() if r["key"] == row["key"]), None)
            reward = [s for s in page.texts if "肥料到账" in s]
            evidence = "次数增加"
            if after is not None:
                if after["current"] <= row["current"]:
                    raise RuntimeError(f"浏览已结束但任务次数未增加：{row['name']}")
                current = after["current"]
            elif row["current"] == row["total"] - 1:
                # 最后一次可能直接移除。再次感知，排除临时未加载。
                time.sleep(1)
                page = self.perceive()
                if not page.task_list or any(r["key"] == row["key"] for r in page.task_rows()):
                    raise RuntimeError("任务消失状态尚未稳定，不能确认完成")
                current = row["total"]
                evidence = "浏览完成文案 + 返回后任务连续两次消失"
            else:
                raise RuntimeError("任务意外消失，无法确认次数")
            result = {"task": row["name"], "key": row["key"], "before": row["current"],
                      "after": current, "total": row["total"], "evidence": evidence,
                      "reward_messages": reward}
            self.results.append(result)
            self.event("任务完成", **result)
        raise RuntimeError("达到执行次数上限")

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
    parser.add_argument("--task-timeout", type=int, default=90, help="单次浏览超时秒数")
    parser.add_argument("--logs", type=Path, default=Path("logs"))
    args = parser.parse_args()
    if args.inspect and args.navigate_only:
        parser.error("--inspect 与 --navigate-only 不能同时使用")
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
            page = runner.bootstrap()
            if not args.navigate_only:
                runner.event("开始做任务")
                runner.execute(page)
    except KeyboardInterrupt:
        error = "用户中止，未核验的任务不计入完成"
    except Exception as exc:
        error = str(exc)
        logging.error("停止：%s", error)
    finally:
        runner.summary(error)
        logging.info("日志目录：%s", directory.resolve())
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())
