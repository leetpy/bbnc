"""扫描覆盖、截图身份及跨核验复用的回归测试，不连接手机。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from farm import Page, Runner
from test_farm import make_page


def screen(rows):
    """构造带任务面板标题的截图识别结果。"""
    return {"width": 1260, "height": 2800, "rows": [
        {"text": "肥料明细", "bounds": [30, 700, 250, 760], "confidence": 1}, *rows]}


def text_row(text, box):
    """为指定文字生成可信OCR行。"""
    return {"text": text, "bounds": box, "confidence": 1}


class ScanReliabilityTests(unittest.TestCase):
    def test_same_name_and_total_are_disambiguated_by_row_reward(self):
        """两个单次任务改为同名后，截图须按各自行内奖励绑定。"""
        xml = (Path(__file__).parent / "tests/fixtures/taobao-single-round-tasks.xml").read_text()
        page = Page(xml.replace("搜一搜你喜欢的商品(0/1)", "发现精选好物(0/1)"))
        page.apply_task_screen(screen([
            text_row("发现精选好物(0/1)", [280, 1000, 800, 1060]),
            text_row("+1500", [550, 1070, 680, 1110]),
            text_row("去完成", [1008, 1004, 1150, 1058]),
            text_row("发现精选好物(0/1)", [280, 1500, 800, 1560]),
            text_row("+150", [550, 1570, 680, 1610]),
            text_row("去完成", [1008, 1504, 1150, 1558])]))
        rows = page.task_rows()
        self.assertTrue(all(page.task_tappable(r["button"]) for r in rows))
        self.assertEqual([r["reward"] for r in rows], ["+1500", "+150"])

    def test_conflicting_screenshot_reward_never_uses_xml_amount(self):
        """截图读到不同奖励时，不得凭旧XML标题匹配并继续点击。"""
        xml = (Path(__file__).parent / "tests/fixtures/taobao-dynamic-selected.xml").read_text()
        page = Page(xml)
        page.apply_task_screen(screen([
            text_row("看看#低糖电饭锅(6/18)", [280, 1000, 800, 1060]),
            text_row("+400", [550, 1070, 680, 1110]),
            text_row("去完成", [1008, 1004, 1150, 1058])]))
        self.assertFalse(page.task_tappable(page.task_rows()[0]["button"]))

    @patch("farm.time.sleep")
    def test_empty_ocr_retries_without_swiping_and_reports_incomplete(self, sleep):
        """空OCR指纹属于识别失败，不能当成两次静止后到达边界。"""
        with tempfile.TemporaryDirectory() as directory:
            page = make_page(current=6)
            page.screen_signature = ()
            runner = Runner(Mock(), Path(directory), 10)
            runner.perceive = Mock(return_value=page)
            scan = {}
            for _ in range(5):
                if runner.scan_task_page(page, scan) is None:
                    break
            self.assertEqual(scan.get("outcome"), "recognition_failed")
            runner.adb.swipe.assert_not_called()
            self.assertEqual(runner.perceive.call_count, 2)
            summary = runner.summary()
            self.assertEqual(summary["status"], "partial")
            self.assertEqual(summary["scan_coverage"]["status"], "incomplete")

    def test_moving_list_limit_is_reported_as_incomplete(self):
        """始终在移动的列表达到14次保护上限时，应报告未确认覆盖。"""
        with tempfile.TemporaryDirectory() as directory:
            page = make_page(current=6)
            runner = Runner(Mock(), Path(directory), 10)
            runner.wait_page = Mock(return_value=page)
            scan = {}
            for index in range(20):
                page.screen_signature = (("任务(0/1)", index * 100),)
                if runner.scan_task_page(page, scan) is None:
                    break
            self.assertEqual(scan.get("outcome"), "limit")
            self.assertEqual(runner.summary()["status"], "partial")

    @patch("farm.time.sleep")
    def test_one_empty_frame_can_recover_without_claiming_a_boundary(self, sleep):
        """短暂漏读后重新采集成功，不能把前后快照直接累计为边界证据。"""
        with tempfile.TemporaryDirectory() as directory:
            empty, recovered = make_page(current=6), make_page(current=6)
            empty.screen_signature = ()
            recovered.screen_signature = (("其他任务(0/1)", 1200),)
            runner = Runner(Mock(), Path(directory), 10)
            runner.perceive = Mock(return_value=recovered)
            scan = {"last": recovered.screen_signature, "repeats": 1}
            self.assertIs(runner.scan_task_page(empty, scan), recovered)
            self.assertEqual(scan["repeats"], 0)
            runner.adb.swipe.assert_not_called()

    def test_summary_separates_pending_skipped_and_unsupported(self):
        """结束原因和未完成任务必须随汇总保存，而不是只有成功结果。"""
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 10)
            page = make_page(current=3)
            runner.observe_tasks(page)
            runner.event("跳过任务", task="看严选推荐商品", reason="入口不可定位")
            summary = runner.summary()
            self.assertEqual(summary["pending_tasks"][0]["current"], 3)
            self.assertEqual(summary["skipped_tasks"][0]["reason"], "入口不可定位")
            self.assertEqual(summary["unsupported_tasks"][0]["task"], "买限时折扣好物得奖励")

    @patch("farm.time.sleep")
    def test_verified_removal_hands_coverage_to_scheduler(self, sleep):
        """最后一条任务移除的完整扫描，后续收尾不能从头再扫一遍。"""
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 10)
            page = make_page(current=6)
            page.screen_rows = []
            page.screen_signature = (("买限时折扣好物得奖励(0/1)", 1000),)
            row = make_page(current=5).task_rows()[0]
            runner.wait_page = Mock(return_value=page)
            runner.verify_task_return(page, row)
            swipes = runner.adb.swipe.call_count
            runner.execute(page)
            self.assertEqual(runner.adb.swipe.call_count, swipes)

    def test_changed_page_invalidates_coverage_handoff(self):
        """核验后出现新任务时，旧覆盖不能使调度器提前结束。"""
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 10)
            page = make_page(current=6)
            page.screen_rows = []
            page.screen_signature = (("其他任务(0/1)", 1000),)
            runner.wait_page = Mock(return_value=page)
            runner.verify_task_return(page, make_page(current=5).task_rows()[0])
            changed = make_page(current=6)
            changed.screen_rows = []
            changed.screen_signature = (("新增任务(0/1)", 1200),)
            self.assertEqual(runner.consume_scan_handoff(changed), {})

    @patch("farm.time.sleep")
    def test_failed_ocr_cannot_prove_task_removal(self, sleep):
        """任务完成浮标之后，列表OCR仍失败时不能把任务缺失判为领奖成功。"""
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 10)
            page = make_page(current=6)
            page.screen_rows = []
            page.screen_signature = ()
            runner.perceive = Mock(return_value=page)
            with self.assertRaisesRegex(RuntimeError, "覆盖未确认"):
                runner.verify_task_return(page, make_page(current=5).task_rows()[0])
            self.assertEqual(runner.results, [])
            runner.adb.swipe.assert_not_called()

    def test_reward_conflict_cannot_prove_task_removal(self):
        """原任务仍出现在截图但奖励冲突时，不能在扫完后把它当作已经移除。"""
        xml = (Path(__file__).parent / "tests/fixtures/taobao-dynamic-selected.xml").read_text()
        page = Page(xml)
        row = page.task_rows()[0]
        page.apply_task_screen(screen([
            text_row("看看#低糖电饭锅(6/18)", [280, 1000, 800, 1060]),
            text_row("+400", [550, 1070, 680, 1110]),
            text_row("去完成", [1008, 1004, 1150, 1058])]))
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 10)
            runner.wait_page = Mock(return_value=page)
            with self.assertRaisesRegex(RuntimeError, "覆盖未确认"):
                runner.verify_task_return(page, row)
            self.assertEqual(runner.scan_reports[-1]["outcome"], "recognition_failed")

    def test_discovered_pending_task_prevents_coverage_reuse(self):
        """移除核验途中看到其他待办，不能复用为“没有任务”的收尾结论。"""
        page = make_page("逛精选好物", current=0, total=1)
        page.apply_task_screen(screen([
            text_row("逛精选好物(0/1)", [280, 1000, 800, 1060]),
            text_row("去完成", [1008, 1004, 1150, 1058])]))
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 10)
            runner.wait_page = Mock(return_value=page)
            runner.verify_task_return(page, make_page(current=5).task_rows()[0])
            self.assertEqual(runner.consume_scan_handoff(page), {})

    def test_expired_handoff_requires_new_scan(self):
        """覆盖证据只在紧接的调度中有效，超时不能沿用。"""
        page = make_page(current=6)
        page.screen_rows = []
        page.screen_signature = (("其他任务(0/1)", 1000),)
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 10)
            runner.wait_page = Mock(return_value=page)
            with patch("farm.time.monotonic", return_value=10):
                runner.verify_task_return(page, make_page(current=5).task_rows()[0])
            with patch("farm.time.monotonic", return_value=21):
                self.assertEqual(runner.consume_scan_handoff(page), {})

    @patch("farm.time.sleep")
    def test_full_last_task_flow_reuses_scan_after_second_confirmation(self, sleep):
        """真实调度链路：浏览、全表移除核验、第二次确认、收尾只进行一次全扫。"""
        before = make_page("逛精选好物", current=0, total=1)
        before.apply_task_screen(screen([
            text_row("逛精选好物(0/1)", [280, 1000, 800, 1060]),
            text_row("去完成", [1008, 1004, 1150, 1058])]))
        after = make_page(current=6)
        after.screen_rows = []
        after.screen_signature = (("其他任务(0/1)", 1000),)
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 10)
            runner.browse = Mock()
            runner.perceive = Mock(return_value=after)
            runner.return_list = Mock(return_value=after)
            runner.wait_page = Mock(return_value=after)
            runner.execute(before)
            self.assertEqual(runner.adb.swipe.call_count, 4)
            self.assertEqual(len(runner.results), 1)
            self.assertEqual(runner.summary()["pending_tasks"], [])
            self.assertEqual(runner.summary()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
