"""首页访问任务与支付宝滚动后旧坐标的回归验证。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, Mock, patch
import xml.etree.ElementTree as ET

from farm import APPS, ALIPAY_TASKS, Page, Runner, TaskUnavailable, bounds


def task_page(key, current=0, button="去完成"):
    """构造同行标题、进度和按钮，沿用实机车险任务的容器结构。"""
    source = (Path(__file__).parent / "tests/fixtures/insurance-before.xml").read_text()
    source = source.replace("搜一搜「车险」", ALIPAY_TASKS[key][0])
    source = source.replace("当前进度0", f"当前进度{current}").replace("去完成", button)
    return Page(source, APPS["alipay"]["package"])


def visit_fixture(name):
    """读取从实机中仅提取相关节点的访问页面，排除账户与消息内容。"""
    return Page((Path(__file__).parent / "tests/fixtures" / f"visit-{name}.xml").read_text(),
                APPS["alipay"]["package"])


class AlipayScreenTests(unittest.TestCase):
    def test_three_visit_tasks_are_separate_from_earning_gold_tasks(self):
        """三个入口独立记账，攒3000豆任务不在执行范围内。"""
        for key in ("home_farm", "home_gold", "gold_visit"):
            self.assertEqual(task_page(key).task_rows()[0]["key"], key)
        source = ET.tostring(task_page("gold_visit").root, encoding="unicode")
        page = Page(source.replace("逛支付宝金豆夺宝", "去金豆夺宝攒3000豆"),
                    APPS["alipay"]["package"])
        self.assertEqual(page.task_rows(), [])

    def test_insurance_zero_coordinates_use_same_screen_title_and_button(self):
        """XML零坐标时只能点击截图确认的同行按钮，不能沿用首屏坐标。"""
        page = task_page("insurance")
        row = page.task_rows()[0]
        for node in (row["title"], row["button"]):
            node.set("bounds", "[0,0][0,0]")
        screen = {"width": 1260, "height": 2800, "rows": [
            {"text": "做任务集肥料", "bounds": [450, 887, 800, 948], "confidence": 1},
            {"text": "搜一搜「车险」 （0/1）", "bounds": [280, 1400, 760, 1450], "confidence": 1},
            {"text": "去完成", "bounds": [975, 1420, 1121, 1473], "confidence": 1},
            {"text": "去完成", "bounds": [975, 1640, 1121, 1693], "confidence": 1}]}
        page.apply_task_screen(screen)
        fresh = page.task_rows()[0]
        self.assertTrue(page.task_tappable(fresh["button"]))
        self.assertNotEqual(bounds(fresh["button"]), (0, 0, 0, 0))
        self.assertFalse(page.task_tappable(row["button"]))

    def test_wrong_application_panel_is_rejected(self):
        """不能把淘宝面板当作支付宝任务列表核验。"""
        with self.assertRaisesRegex(RuntimeError, "任务面板"):
            task_page("insurance").apply_task_screen({"width": 1260, "height": 2800,
                "rows": [{"text": "肥料明细", "bounds": [0, 0, 100, 100], "confidence": 1}]})

    def test_real_guides_home_and_gold_are_distinct(self):
        """金豆引导遮挡列表，首页服务示意图不应识别为原生首页。"""
        self.assertFalse(visit_fixture("gold-guide").task_list)
        self.assertFalse(visit_fixture("farm-service").alipay_home)
        self.assertTrue(visit_fixture("farm-service").farm_home_service)
        home = visit_fixture("alipay-home")
        self.assertTrue(home.alipay_home)
        self.assertIsNotNone(home.common_app("芭芭农场"))
        self.assertIsNone(visit_fixture("farm-service").common_app("芭芭农场"))
        self.assertTrue(visit_fixture("gold-venue").gold_venue)


class AlipayVisitTests(unittest.TestCase):
    @patch("farm.time.sleep")
    def test_all_three_routes_return_verify_and_claim(self, sleep):
        """回放各自的真实目的页，必须完成进度核验后才调用领奖流程。"""
        for key in ("home_farm", "gold_visit", "home_gold"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                runner = Runner(Mock(), Path(directory), 10, app="alipay")
                before, ready = task_page(key), task_page(key, 1, "领取")
                destination = visit_fixture("farm-service" if key == "home_farm" else "gold-venue")
                if key == "home_gold":
                    runner.wait_page = Mock(side_effect=[visit_fixture("gold-guide"),
                        visit_fixture("alipay-home"), destination])
                    runner.open_common_app = Mock()
                else:
                    runner.wait_page = Mock(return_value=destination)
                runner.return_alipay_farm = Mock(return_value=ready)
                runner.claim_alipay = Mock(return_value=ready)
                self.assertIs(runner.execute_alipay_visit(before, key), ready)
                runner.claim_alipay.assert_called_once_with(ready, key, 0, "去完成",
                    allow_removed=True, visit_route=key, visit_verified=True)
                runner.adb.swipe.assert_not_called()
                self.assertEqual(runner.adb.tap.call_count, 1 if key == "gold_visit" else 2)
                if key == "home_gold":
                    runner.open_common_app.assert_called_once_with(
                        ANY, "金豆夺宝")

    def test_ready_tasks_only_claim_and_completed_tasks_skip(self):
        """已经完成的访问不重复打开，已完成待领取时只领奖。"""
        for key in ("home_farm", "gold_visit", "home_gold"):
            with tempfile.TemporaryDirectory() as directory:
                runner = Runner(Mock(), Path(directory), 10, app="alipay")
                ready = task_page(key, 1, "领取")
                runner.claim_alipay = Mock(return_value=ready)
                runner.execute_alipay_visit(ready, key)
                runner.claim_alipay.assert_called_once()
                runner.adb.tap.assert_not_called()
                done = task_page(key, 1, "已领取")
                runner.claim_alipay.reset_mock()
                self.assertIs(runner.execute_alipay_visit(done, key), done)
                runner.claim_alipay.assert_not_called()

    @patch("farm.time.sleep")
    def test_unchanged_progress_is_not_claimed(self, sleep):
        """到达会场不能替代农场进度；一直0/1时停止且不领奖。"""
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 10, app="alipay")
            before = task_page("gold_visit")
            runner.wait_page = Mock(return_value=visit_fixture("gold-venue"))
            runner.return_alipay_farm = Mock(return_value=before)
            runner.perceive = Mock(return_value=before)
            with self.assertRaisesRegex(RuntimeError, "进度未完成"):
                runner.execute_gold_visit(before)
            self.assertEqual(runner.results, [])
            runner.adb.tap.assert_called_once()

    def test_missing_home_gold_entry_does_not_search_or_click_ads(self):
        """首页找不到金豆时，只检查常用全部列表，不使用搜索或其他活动入口。"""
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 10, app="alipay")
            common = Page('<hierarchy><node package="com.eg.android.AlipayGphone" '
                'text="最近常用应用" bounds="[0,0][1080,2400]"/></hierarchy>',
                APPS["alipay"]["package"])
            runner.wait_page = Mock(return_value=common)
            with self.assertRaisesRegex(RuntimeError, "未找到金豆夺宝"):
                runner.open_common_app(visit_fixture("alipay-home"), "金豆夺宝")
            runner.adb.tap.assert_called_once()

    def test_missing_home_gold_returns_to_farm_before_skipping(self):
        """缺少常用入口不能阻塞其他农场任务，异常必须携带已找回的列表。"""
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 10, app="alipay")
            before = task_page("home_gold")
            runner.wait_page = Mock(return_value=visit_fixture("alipay-home"))
            runner.open_common_app = Mock(side_effect=TaskUnavailable("缺少金豆入口", before))
            runner.return_alipay_farm = Mock(return_value=before)
            runner.claim_alipay = Mock()
            with self.assertRaises(TaskUnavailable) as caught:
                runner.execute_home_gold(before)
            self.assertIs(caught.exception.page, before)
            runner.return_alipay_farm.assert_called_once()
            runner.claim_alipay.assert_not_called()


if __name__ == "__main__":
    unittest.main()
