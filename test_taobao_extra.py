"""淘宝新增活动任务、面板裁剪与底部推广遮挡的回归测试。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from farm import Page, Runner


class TaobaoExtraTests(unittest.TestCase):
    def setUp(self):
        # 这些用例只验证任务处理流程；完整逐屏扫描由独立调度回归覆盖。
        scan = patch.object(Runner, "scan_task_page", return_value=None)
        self.scan_page = scan.start()
        self.addCleanup(scan.stop)

    def test_takeaway_task_returns_after_browsing_and_verifies_removal(self):
        # 仅验证外卖会场浏览奖励，不点击店铺、加号或下单入口。
        directory_path = Path(__file__).parent / 'tests/fixtures'
        before = Page((directory_path / 'taobao-takeaway-before.xml').read_text())
        self.assertEqual(len(before.task_rows()), 1)
        row = before.task_rows()[0]
        self.assertEqual((row['key'], row['reward'], row['total']), ('takeaway', '+300', 1))
        self.assertEqual(row['button'].get('text'), '去完成')
        root = ET.fromstring(ET.tostring(before.root, encoding='unicode'))
        root[0].remove(root[0][-1])
        ET.SubElement(root[0], 'node', text='已完成')
        after = Page(ET.tostring(root, encoding='unicode'))
        with tempfile.TemporaryDirectory() as directory, patch('farm.time.sleep'), \
                patch.object(Page, 'task_tappable', return_value=True):
            adb = Mock()
            runner = Runner(adb, Path(directory), 10)
            done = Page((directory_path / 'taobao-takeaway-done.xml').read_text())
            runner.perceive = Mock(side_effect=[done, after])
            runner.return_list = Mock(return_value=after)
            runner.execute(before)
            self.assertEqual(len(runner.results), 1)
            self.assertEqual(runner.results[0]['after'], 1)
            adb.tap.assert_called_once()
            adb.swipe.assert_not_called()  # 已读到实机完成浮标，不再继续滑动。

    def page(self):
        """读取只保留四条指定任务的实机列表快照。"""
        return Page((Path(__file__).parent / 'tests/fixtures/taobao-extra-tasks.xml').read_text())

    def test_four_tasks_and_ip_button_are_recognized(self):
        rows = self.page().task_rows()
        self.assertEqual([r['key'] for r in rows], ['coins', 'ip', 'domestic', 'saving_card'])
        self.assertEqual(rows[1]['button'].get('text'), '去逛逛')
        self.assertEqual([r['reward'] for r in rows], ['+300', '+300', '+200', '+300'])

    def test_ip_only_list_and_real_completion_signals(self):
        # 任务仅剩“去逛逛”入口时，仍能识别列表；实机完成浮标无需额外点击。
        page = self.page()
        row = page.task_rows()[1]
        container = page.parents[page.parents[page.parents[row['title']]]]
        root = ET.Element('hierarchy')
        window = ET.SubElement(root, 'node', bounds='[0,0][1260,2800]')
        ET.SubElement(window, 'node', text='肥料明细')
        window.append(container)
        self.assertTrue(Page(ET.tostring(root, encoding='unicode')).task_list)
        for task in ('coins', 'ip', 'saving-card', 'domestic', 'takeaway'):
            with self.subTest(task=task), tempfile.TemporaryDirectory() as directory:
                done = Page((Path(__file__).parent / f'tests/fixtures/taobao-{task}-done.xml').read_text())
                adb = Mock()
                Runner(adb, Path(directory), 10).browse(done)
                adb.tap.assert_not_called()
                adb.swipe.assert_not_called()

    def test_unverified_coins_titles_are_not_broadly_whitelisted(self):
        xml = ET.tostring(self.page().root, encoding='unicode')
        page = Page(xml.replace('双11来淘金币狂享20亿', '双11来淘金币狂享下单奖励'))
        self.assertNotIn('coins', [r['key'] for r in page.task_rows()])

    def test_coins_amount_change_keeps_identity(self):
        before = self.page()
        xml = ET.tostring(before.root, encoding='unicode').replace('狂享20亿(0/1)', '狂享30亿(1/1)')
        row = Runner.task_row(Page(xml), before.task_rows()[0])
        self.assertEqual(row['current'], 1)

    def test_each_task_removed_after_completion_is_verified(self):
        # 模拟浏览成功后单次任务移除；沿用两次列表核验，不直接把消失当成功。
        for key in ('coins', 'ip', 'domestic', 'saving_card'):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory, \
                    patch('farm.time.sleep'), patch.object(Page, 'task_tappable', return_value=True):
                before = self.page()
                row = next(r for r in before.task_rows() if r['key'] == key)
                parent = before.parents[before.parents[before.parents[row['title']]]]
                before.parents[parent].remove(parent)
                after = Page(ET.tostring(before.root, encoding='unicode'))
                adb = Mock()
                runner = Runner(adb, Path(directory), 10)
                runner.browse = Mock()
                runner.perceive = Mock(return_value=after)
                runner.return_list = Mock(return_value=after)
                runner.execute(self.page(), only=(key, 1))
                self.assertEqual([r['key'] for r in runner.results], [key])
                adb.tap.assert_called_once()

    def test_screenshot_recovers_domestic_task_from_stale_xml(self):
        # 实机滚动后 XML 仍停留在初始位置，截图已将国货任务滚到中部。
        page = Page('<hierarchy><node bounds="[0,0][1260,2800]">'
                    '<node text="肥料明细"/><node bounds="[0,678][1260,2738]">'
                    '<node class="android.widget.ListView" bounds="[27,0][1238,2470]">'
                    '<node><node text="浏览国货严选红包抵扣(0/1)" bounds="[283,2514][870,2585]"/>'
                    '<node text="+200"/><node text="去完成" clickable="true" '
                    'bounds="[958,2558][1198,2662]"/></node></node></node>'
                    '<node content-desc="跳转链接" clickable="true" bounds="[27,2494][1238,2703]"/>'
                    '</node></hierarchy>')
        self.assertFalse(page.task_tappable(page.task_rows()[0]["button"]))
        screen = {"width": 1260, "height": 2800, "rows": [
            {"text": "做任务赚肥料", "confidence": 1, "bounds": [390,561,877,651]},
            {"text": "浏览国货严选红包抵扣（0/1）", "confidence": 1,
             "bounds": [284,1086,873,1139]},
            {"text": "去完成", "confidence": 1, "bounds": [1008,1147,1150,1197]}]}
        page.apply_task_screen(screen)
        row = page.task_rows()[0]
        self.assertEqual(row["key"], "domestic")
        self.assertEqual(row["button"].get("bounds"), "[1008,1147][1150,1197]")
        self.assertTrue(page.task_tappable(row["title"]))
        self.assertTrue(page.task_tappable(row["button"]))
        # 截图中未出现的旧任务不能继续参与调度，也不能错误确认奖励。
        screen["rows"] = screen["rows"][:1]
        page.apply_task_screen(screen)
        self.assertFalse(page.task_tappable(page.task_rows()[0]["button"]))

    def test_return_verification_searches_offscreen_task_before_using_progress(self):
        # 当前截图未看到任务时，旧 XML 的 0/1 既不能导致误报，也不能当成任务移除。
        first, second = self.page(), self.page()
        row = next(r for r in first.task_rows() if r["key"] == "domestic")
        header = {"text":"做任务赚肥料","confidence":1,"bounds":[390,561,877,651]}
        first.apply_task_screen({"width":1260,"height":2800,"rows":[header,
            {"text":"买限时折扣好物得奖励(0/1)","confidence":1,"bounds":[284,800,873,850]}]})
        second.apply_task_screen({"width":1260,"height":2800,"rows":[header,
            {"text":"浏览国货严选红包抵扣(1/1)","confidence":1,
             "bounds":[284,1086,873,1139]},
            {"text":"已完成","confidence":1,"bounds":[1008,1147,1150,1197]}]})
        self.assertIsNone(Runner.task_row(first, row))
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 10)
            self.scan_page.return_value = second
            page, after = runner.verify_task_return(first, row)
            self.assertIs(page, second)
            self.assertEqual(after["current"], 1)
            self.scan_page.assert_called_once()

    def test_screen_requires_matching_panel_and_same_row_button(self):
        page = self.page()
        with self.assertRaisesRegex(RuntimeError, "任务面板"):
            page.apply_task_screen({"width":1260,"height":2800,"rows":[]})
        screen = {"width":1260,"height":2800,"rows":[
            {"text":"肥料明细","confidence":1,"bounds":[30,580,230,630]},
            {"text":"浏览国货严选红包抵扣(0/1)","confidence":1,
             "bounds":[284,1086,873,1139]},
            {"text":"去完成","confidence":1,"bounds":[1008,1371,1150,1424]}]}
        page.apply_task_screen(screen)
        self.assertFalse(any(page.task_tappable(r["button"]) for r in page.task_rows()))

    def test_footer_overlay_blocks_task_button(self):
        # 横幅与任务按钮在不同分支，二者的坐标可以同时落在屏幕内。
        page = Page('''<hierarchy><node bounds="[0,0][1260,2800]">
          <node text="去完成" bounds="[958,2558][1198,2662]"/>
          <node content-desc="跳转链接" clickable="true" bounds="[27,2494][1238,2703]"/>
        </node></hierarchy>''')
        button = page.exact('去完成')
        self.assertTrue(page.visible(button))
        self.assertFalse(page.task_tappable(button))
        page.nodes[-1].set('bounds', '[27,2700][1238,2750]')
        self.assertTrue(page.task_tappable(button))

    def test_parent_clip_blocks_off_panel_task(self):
        page = Page('''<hierarchy><node bounds="[0,0][1260,2800]">
          <node bounds="[0,678][1260,2738]">
            <node text="去完成" bounds="[958,37][1198,141]"/>
          </node></node></hierarchy>''')
        self.assertFalse(page.task_tappable(page.exact('去完成')))

    def test_stale_list_coordinates_block_even_in_screen_child(self):
        # 按钮本身在面板内，但整个列表已被错误偏移；不能用其旧坐标点其他行。
        page = Page('''<hierarchy><node bounds="[0,0][1260,2800]">
          <node bounds="[0,678][1260,2738]">
            <node class="android.widget.ListView" bounds="[27,0][1238,2467]">
              <node text="去完成" bounds="[958,712][1198,820]"/>
            </node></node></node></hierarchy>''')
        self.assertFalse(page.task_tappable(page.exact('去完成')))

    def test_list_bottom_clipping_does_not_block_visible_button(self):
        # 初始列表底部可比 WebView 多 1px；不能因此把所有正常入口都跳过。
        page = Page('''<hierarchy><node bounds="[0,0][1260,2800]">
          <node bounds="[0,675][1260,2737]">
            <node class="android.widget.ListView" bounds="[27,722][1238,2738]">
              <node text="去完成" bounds="[958,1204][1198,1309]"/>
            </node></node></node></hierarchy>''')
        self.assertTrue(page.task_tappable(page.exact('去完成')))

    def test_domestic_reward_panel_is_done_but_ads_are_not(self):
        xml = (Path(__file__).parent / 'tests/fixtures/taobao-domestic-done.xml').read_text()
        self.assertEqual(Page(xml).browse_state(), 'done')
        # 缺少到账或弹窗倒计时，仅有下单广告不足以确认任务完成。
        self.assertEqual(Page(xml.replace('200肥料已到账', '下单得200肥料')).browse_state(), 'unknown')
        self.assertEqual(Page(xml.replace('29s后奖励消失', '')).browse_state(), 'unknown')


if __name__ == '__main__':
    unittest.main()
