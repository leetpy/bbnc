"""淘宝动态商品标题的实机快照及返回核验回归测试。"""
from pathlib import Path
import copy
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from farm import Page, Runner


def source():
    """只读取失败现场的任务行，保留实际的裁剪坐标、浏览说明与奖励。"""
    return (Path(__file__).parent / "tests/fixtures/taobao-dynamic-selected.xml").read_text()


class TaobaoDynamicTests(unittest.TestCase):
    def test_real_dynamic_title_keeps_family_progress_and_reward(self):
        """6/18任务须保留在推荐浏览族内，不能因改成商品名而消失。"""
        rows = Page(source()).task_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["key"], row["current"], row["total"], row["reward"]),
                         ("selected", 6, 18, "+900"))
        before = Page(source().replace("看看#低糖电饭锅(6/18)", "发现精选好物(5/18)"))
        self.assertEqual(Runner.task_row(Page(source()), before.task_rows()[0])["current"], 6)

    def test_screenshot_locates_dynamic_row_with_invalid_xml_coordinates(self):
        """保留失真的XML边界，由同张截图的动态标题和同行按钮恢复可点击位置。"""
        page = Page(source())
        page.apply_task_screen({"width": 1260, "height": 2800, "rows": [
            {"text": "肥料明细", "bounds": [60, 700, 260, 760], "confidence": 1},
            {"text": "看看#低糖电饭锅（6/18）", "bounds": [280, 1000, 800, 1060], "confidence": 1},
            {"text": "去完成", "bounds": [1008, 1004, 1150, 1058], "confidence": 1}]})
        row = page.task_rows()[0]
        self.assertTrue(page.task_tappable(row["title"]))
        self.assertTrue(page.task_tappable(row["button"]))
        self.assertEqual(row["current"], 6)

    def test_dynamic_name_requires_browse_instruction_reward_and_button(self):
        """商品标题不能单凭前缀进入白名单，必须有同行浏览任务的完整证据。"""
        for old, new in (("浏览15秒得", "下单得奖励"), ("+900", "奖励未知"),
                         ("去完成", "去购买"), ("看看#低糖电饭锅", "看看低糖电饭锅"),
                         ("看看#低糖电饭锅", "看看#")):
            with self.subTest(old=old):
                self.assertEqual(Page(source().replace(old, new)).task_rows(), [])

    def test_single_round_dynamic_name_uses_same_normalization(self):
        """单次动态标题仍按单次浏览身份核验，不与多次推荐任务混用。"""
        row = Page(source().replace("(6/18)", "(0/1)")).task_rows()[0]
        self.assertEqual((row["key"], row["total"]), ("browse15", 1))

    def test_reward_mismatch_or_ambiguous_rows_do_not_match(self):
        """动态标题不能绕过奖励身份；同次数同奖励有歧义时仍须停止。"""
        before = Page(source().replace("看看#低糖电饭锅(6/18)", "发现精选好物(5/18)"))
        row = before.task_rows()[0]
        self.assertIsNone(Runner.task_row(Page(source().replace("+900", "+400")), row))
        ambiguous = Page(source())
        ambiguous.root[0].append(copy.deepcopy(ambiguous.root[0][-1]))
        ambiguous = Page(ET.tostring(ambiguous.root, encoding="unicode"))
        with self.assertRaisesRegex(RuntimeError, "任务身份不唯一"):
            Runner.task_row(ambiguous, row)

    @patch("farm.time.sleep")
    @patch.object(Page, "task_tappable", return_value=True)
    @patch.object(Runner, "scan_task_page", return_value=None)
    def test_scheduler_continues_through_dynamic_renames(self, scan, tappable, sleep):
        """回放5/18返回动态6/18后继续直到18/18，进度与奖励身份保持一致。"""
        before = Page(source().replace("看看#低糖电饭锅(6/18)", "发现精选好物(5/18)"))
        after = [Page(source().replace("(6/18)", f"({i}/18)").replace(
            "看看#低糖电饭锅", "看看#空气炸锅" if i % 2 else "看看#低糖电饭锅"))
            for i in range(6, 19)]
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 10)
            runner.perceive = Mock(return_value=before)
            runner.browse = Mock()
            runner.return_list = Mock(side_effect=after)
            runner.execute(before, only=("selected", 18))
            self.assertEqual([r["after"] for r in runner.results], list(range(6, 19)))
            self.assertEqual(runner.results[0]["task_after"], "看看#低糖电饭锅")
            self.assertEqual(runner.adb.tap.call_count, 13)


if __name__ == "__main__":
    unittest.main()
