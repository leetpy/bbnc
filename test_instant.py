"""支付宝单次立得任务：实机 XML 夹具回放，所有设备动作均使用 Mock。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from farm import APPS, Page, Runner, TaskUnavailable


def fixture(stage):
    return Page((Path(__file__).parent / "tests/fixtures" / f"instant-{stage}.xml").read_text(encoding="utf-8"),
                APPS["alipay"]["package"])


class InstantTests(unittest.TestCase):
    def test_dynamic_amount_alias_and_separate_task_identity(self):
        source = (Path(__file__).parent / "tests/fixtures/instant-before.xml").read_text(encoding="utf-8")
        for name in ("逛好物立得1000肥料", "浏览好物立得2000肥料"):
            page = Page(source.replace("逛好物立得1000肥料", name), APPS["alipay"]["package"])
            row = next(r for r in page.task_rows() if r["key"] == "instant")
            self.assertEqual(row["name"], name)
            self.assertEqual((row["current"], row["total"]), (0, 1))
            self.assertEqual(row["button"].get("text"), "去完成")
            self.assertEqual(len([r for r in page.task_rows() if r["key"] == "goodies"]), 1)

    def test_real_countdown_and_issued_badge(self):
        self.assertEqual(fixture("browsing").instant_state()["remaining"], 15)
        self.assertEqual(fixture("countdown").instant_state()["remaining"], 2)
        issued = fixture("issued").instant_state()
        self.assertTrue(issued["issued"])
        self.assertEqual(issued["signals"], ["1000肥料", "已发放"])
        self.assertEqual(issued["amount"], 1000)  # 浮标镜像/动画不重复统计金额。

    def test_zero_seconds_or_external_ad_does_not_prove_completion(self):
        source = (Path(__file__).parent / "tests/fixtures/instant-browsing.xml").read_text(encoding="utf-8")
        page = Page(source.replace("滑动浏览15秒", "滑动浏览0秒"), APPS["alipay"]["package"])
        self.assertFalse(page.instant_state()["issued"])
        self.assertEqual(page.instant_state()["remaining"], 0)

    def test_badge_disappearing_is_not_completion(self):
        page = fixture("browsing")
        header = next(n for n in page.nodes if n.get("text") == "浏览15s得1000肥料")
        body = page.parents[page.parents[header]]
        body.remove(body[1])
        page = Page(ET.tostring(page.root, encoding="unicode"), APPS["alipay"]["package"])
        self.assertIsNone(page.instant_state())

    def test_complete_flow_scrolls_and_checks_farm_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            runner.wait_page = Mock(return_value=fixture("browsing"))
            runner.perceive = Mock(side_effect=[fixture("countdown"), fixture("issued")])
            runner.return_list = Mock(return_value=fixture("ready"))
            runner.execute_instant(fixture("before"))
            self.assertEqual(len(runner.results), 1)
            result = runner.results[0]
            self.assertEqual((result["before"], result["after"]), (0, 1))
            self.assertEqual(result["button_after"], "继续逛")
            self.assertEqual(result["displayed_amount"], 1000)
            self.assertEqual(result["browse_signals"], ["1000肥料", "已发放"])
            adb.tap.assert_called_once()  # 只点去完成，不点商品或额外领奖。
            self.assertEqual(adb.swipe.call_count, 2)

    def test_returned_progress_not_complete_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            runner.wait_page = Mock(return_value=fixture("issued"))
            runner.return_list = Mock(return_value=fixture("before"))
            with self.assertRaisesRegex(RuntimeError, "任务进度未完成"):
                runner.execute_instant(fixture("before"))
            self.assertEqual(runner.results, [])

    def test_completed_continue_row_is_skipped_without_reopening(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            runner.execute_instant(fixture("ready"))
            adb.tap.assert_not_called()
            adb.swipe.assert_not_called()
            self.assertEqual(runner.results, [])

    def test_initially_issued_badge_is_not_new_reward(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            runner.wait_page = Mock(return_value=fixture("issued"))
            runner.return_list = Mock(return_value=fixture("ready"))
            runner.execute_instant(fixture("before"))
            self.assertEqual(runner.results, [])
            adb.swipe.assert_not_called()

    def test_ready_claim_delegates_to_shared_claim_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            page = fixture("ready")
            row = next(r for r in page.task_rows() if r["key"] == "instant")
            row["button"].set("text", "领取")
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            runner.claim_alipay = Mock(return_value=page)
            self.assertIs(runner.execute_instant(page), page)
            runner.claim_alipay.assert_called_once_with(page, "instant", 1, "领取", allow_removed=True)
            adb.tap.assert_not_called()

    def test_final_task_removal_requires_stable_list(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"):
            before = fixture("before")
            after = fixture("ready")
            row = next(r for r in after.task_rows() if r["key"] == "instant")
            after.root[0].remove(after.parents[row["title"]])
            after = Page(ET.tostring(after.root, encoding="unicode"), APPS["alipay"]["package"])
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            runner.wait_page = Mock(return_value=fixture("browsing"))
            issued = fixture("issued")
            runner.browse_instant = Mock(return_value=(issued, issued.instant_state()))
            runner.return_list = Mock(return_value=after)
            runner.perceive = Mock(return_value=after)
            runner.execute_instant(before)
            self.assertEqual(runner.results[0]["after"], 1)
            self.assertEqual(runner.results[0]["button_after"], "任务已移除")
            runner.perceive.assert_called_once()

    def test_timeout_and_amount_mismatch_do_not_click_or_record_success(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.monotonic", side_effect=[0, 0, 11]):
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            runner.perceive = Mock(return_value=fixture("browsing"))
            with self.assertRaisesRegex(RuntimeError, "浏览超时"):
                runner.browse_instant(fixture("browsing"), 1000)
            self.assertEqual(runner.results, [])
            adb.tap.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            with self.assertRaisesRegex(RuntimeError, "金额不一致"):
                runner.browse_instant(fixture("issued"), 2000)
            adb.swipe.assert_not_called()

    def test_missing_or_unavailable_instant_does_not_block_other_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            empty = Page('<hierarchy><node package="com.eg.android.AlipayGphone"/></hierarchy>',
                         APPS["alipay"]["package"])
            runner.execute_instant(empty)
            adb.tap.assert_not_called()
            page = fixture("before")
            runner.execute_goodies = Mock(return_value=page)
            runner.execute_instant = Mock(side_effect=TaskUnavailable("任务不可定位", page))
            runner.execute_insurance = Mock(return_value=page)
            runner.execute_saving = Mock(return_value=page)
            runner.execute(page)
            runner.execute_insurance.assert_not_called()
            runner.execute_saving.assert_not_called()


if __name__ == "__main__":
    unittest.main()
