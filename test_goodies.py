"""支付宝分段浏览奖励回归测试：夹具来自实机，设备操作全部使用 Mock。"""
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from farm import APPS, Page, Runner, TaskUnavailable


def fixture(name):
    return Page((Path(__file__).parent / "tests/fixtures" / f"goodies-{name}.xml").read_text(),
                APPS["alipay"]["package"])


def panel(*statuses):
    """在实机面板结构上模拟状态变化，保留另一个重复顶栏以测试去重。"""
    page = fixture("entry")
    label = next(n for n in page.nodes if n.get("text") == "每浏览15秒可得1次奖励")
    root = page.parents[label]
    stages = [c for c in root if any(n.get("text", "").isdecimal() for n in c.iter("node"))]
    for index, stage in enumerate(stages):
        if index >= len(statuses):
            root.remove(stage)
            continue
        status = next(n for n in stage.iter("node")
                      if re.fullmatch(r"\d+s后领取|第\d+次", n.get("text", "")))
        status.set("text", statuses[index])
    return Page(ET.tostring(page.root, encoding="unicode"), APPS["alipay"]["package"])


class GoodiesTests(unittest.TestCase):
    def test_task_amount_is_dynamic_and_other_family_is_separate(self):
        for amount in (1500, 2500):
            source = (Path(__file__).parent / "tests/fixtures/goodies-before.xml").read_text()
            page = Page(source.replace("最高得1500", f"最高得{amount}"), APPS["alipay"]["package"])
            rows = [r for r in page.task_rows() if r["key"] == "goodies"]
            self.assertEqual(len(rows), 1)  # 单次“立得”与分段“最高得”各用独立身份。
            self.assertEqual(rows[0]["key"], "goodies")
            self.assertEqual(rows[0]["name"], f"逛好物最高得{amount}肥料")
            self.assertEqual(rows[0]["button"].get("text"), "去逛逛")

    def test_completed_farm_progress_can_still_have_continue_button(self):
        row = fixture("continue").task_rows()[0]
        self.assertEqual((row["current"], row["total"]), (1, 1))
        self.assertEqual(row["button"].get("text"), "继续逛")

    def test_real_initial_first_and_paused_states(self):
        self.assertEqual(fixture("entry").goodies_state()["claimed"], 0)
        self.assertEqual(fixture("first").goodies_state()["claimed"], 1)
        paused = fixture("paused").goodies_state()
        self.assertEqual(len(paused["stages"]), 2)
        self.assertEqual(paused["stages"][1]["status"], "滑动以计时")
        self.assertFalse(paused["all_claimed"])

    def test_unrecognized_pending_status_is_not_dropped(self):
        state = panel("已领取", "奖励加载中").goodies_state()
        self.assertEqual(len(state["stages"]), 2)
        self.assertFalse(state["all_claimed"])

    def test_duplicate_header_and_advertisements_do_not_add_rewards(self):
        state = fixture("entry").goodies_state()
        self.assertEqual(len(state["stages"]), 2)
        self.assertEqual(state["claimed"], 0)  # 夹具外部另有“已领取”广告文案。

    def test_real_completed_page_needs_no_reward_panel(self):
        state = fixture("completed").goodies_state()
        self.assertTrue(state["completed"])
        self.assertEqual(state["completion"], "今日任务已完成")
        self.assertEqual(state["stages"], [])

    def test_completed_text_in_ad_is_not_completion(self):
        page = fixture("entry")
        ET.SubElement(page.root[0], "node", text="今日任务已完成")
        page = Page(ET.tostring(page.root, encoding="unicode"), APPS["alipay"]["package"])
        self.assertFalse(page.goodies_state()["completed"])
        self.assertFalse(page.goodies_state()["all_claimed"])

    def test_transition_to_completed_page_keeps_observed_reward_evidence(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"):
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            done = fixture("completed")
            runner.perceive = Mock(side_effect=[fixture("first"), done, done])
            _, details = runner.browse_goodies(fixture("entry"))
            self.assertEqual(details["completion"], "今日任务已完成")
            self.assertEqual(details["claimed_after"], 1)  # 不按标题上限推算未见到的奖励。
            self.assertEqual(len(details["new_rewards"]), 1)
            adb.tap.assert_not_called()

    def test_browse_tracks_new_rewards_without_clicking_products(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"):
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            done = panel("已领取", "已领取")
            runner.perceive = Mock(side_effect=[fixture("first"), done, done])
            _, details = runner.browse_goodies(fixture("entry"))
            self.assertEqual((details["claimed_before"], details["claimed_after"]), (0, 2))
            self.assertEqual([r["stage"] for r in details["new_rewards"]], [1, 2])
            self.assertEqual(adb.swipe.call_count, 2)
            adb.tap.assert_not_called()

    def test_temporarily_missing_pending_slot_does_not_finish_early(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"):
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            done = panel("已领取", "已领取")
            runner.perceive = Mock(side_effect=[panel("已领取"), done, done])
            _, details = runner.browse_goodies(fixture("paused"))
            self.assertEqual(details["claimed_after"], 2)
            self.assertEqual(adb.swipe.call_count, 2)

    def test_timeout_does_not_mark_browse_success(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"), \
                patch("farm.time.monotonic", side_effect=[0, 0, 11]):
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            runner.perceive = Mock(return_value=fixture("entry"))
            with self.assertRaisesRegex(RuntimeError, "浏览超时"):
                runner.browse_goodies(fixture("entry"))
            self.assertEqual(runner.results, [])
            adb.tap.assert_not_called()

    def test_already_claimed_rewards_are_not_counted_again(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"):
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            done = panel("已领取", "已领取")
            runner.wait_page = Mock(return_value=done)
            runner.perceive = Mock(return_value=done)
            runner.return_list = Mock(return_value=fixture("continue"))
            runner.execute_goodies(fixture("continue"))
            self.assertEqual(runner.results, [])
            adb.swipe.assert_not_called()

    def test_real_daily_completed_page_is_skipped_without_swiping(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"):
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            done = fixture("completed")
            runner.wait_page = Mock(return_value=done)
            runner.perceive = Mock(return_value=done)
            runner.return_list = Mock(return_value=fixture("continue"))
            runner.execute_goodies(fixture("continue"))
            self.assertEqual(runner.results, [])
            adb.swipe.assert_not_called()
            adb.tap.assert_called_once()  # 只打开已完成页核对，不点击广告或额外领取。

    def test_continuation_without_observed_new_award_is_not_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 10, app="alipay")
            runner.wait_page = Mock(return_value=fixture("paused"))
            runner.browse_goodies = Mock(return_value=(fixture("completed"),
                {"new_rewards": [], "claimed_before": 1, "claimed_after": 1,
                 "reward_stages": [], "completion": "今日任务已完成"}))
            runner.return_list = Mock(return_value=fixture("continue"))
            runner.execute_goodies(fixture("continue"))
            self.assertEqual(runner.results, [])

    def test_automatic_award_is_verified_back_in_farm(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            details = {"reward_stages": [], "new_rewards": [{"stage": 1}],
                       "claimed_before": 0, "claimed_after": 2}
            runner.wait_page = Mock(return_value=fixture("entry"))
            runner.browse_goodies = Mock(return_value=(panel("已领取", "已领取"), details))
            runner.return_list = Mock(return_value=fixture("continue"))
            runner.execute_goodies(fixture("before"))
            self.assertEqual(len(runner.results), 1)
            self.assertEqual(runner.results[0]["key"], "goodies")
            self.assertEqual(runner.results[0]["after"], 1)
            adb.tap.assert_called_once()  # 仅入口，不点击页面内其他领取或商品。

    def test_missing_task_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            page = Page('<hierarchy><node package="com.eg.android.AlipayGphone"/></hierarchy>',
                        APPS["alipay"]["package"])
            self.assertIs(runner.execute_goodies(page), page)
            adb.tap.assert_not_called()
            self.assertEqual(runner.results, [])

    def test_unavailable_goodies_does_not_block_other_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            page = fixture("before")
            runner = Runner(Mock(), Path(directory), 10, app="alipay")
            runner.execute_goodies = Mock(side_effect=TaskUnavailable("任务不可定位", page))
            runner.execute_instant = Mock(return_value=page)
            runner.execute_insurance = Mock(return_value=page)
            runner.execute_saving = Mock(return_value=page)
            runner.execute(page)
            runner.execute_instant.assert_called_once_with(page)
            runner.execute_insurance.assert_called_once_with(page)
            runner.execute_saving.assert_called_once_with(page)


if __name__ == "__main__":
    unittest.main()
