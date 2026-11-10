"""支付宝精选商品多轮浏览回归，入口结构取自实机。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from farm import APPS, Page, Runner, TaskUnavailable
from test_goodies import fixture as goodies_fixture


def farm_page(current=0, total=3, button='去完成'):
    """仅改变实机任务进度和按钮，保留同行按钮定位结构。"""
    xml = (Path(__file__).parent / 'tests/fixtures/featured-before.xml').read_text()
    xml = xml.replace('当前进度0，总进度3', f'当前进度{current}，总进度{total}')
    return Page(xml.replace('去完成', button), APPS['alipay']['package'])


class FeaturedTests(unittest.TestCase):
    def test_real_task_identity_and_button(self):
        row = farm_page().task_rows()[0]
        self.assertEqual((row['key'], row['current'], row['total']), ('featured', 0, 3))
        self.assertEqual(row['button'].get('text'), '去完成')

    def runner(self, directory):
        """用真实任务行和浏览面板搭建无设备回归，浏览推进单独由面板用例覆盖。"""
        runner = Runner(Mock(), Path(directory), 90, app='alipay')
        runner.wait_page = Mock(return_value=goodies_fixture('entry'))
        runner.browse_goodies = Mock(return_value=(goodies_fixture('completed'),
            {'new_rewards': [{'stage': 1, 'displayed_amount': 500}], 'completion': '今日任务已完成'}))
        return runner

    def test_real_rewards_panel_and_claimed_all_terminal(self):
        directory = Path(__file__).parent / 'tests/fixtures'
        before = Page((directory / 'featured-entry.xml').read_text(), APPS['alipay']['package'])
        after = Page((directory / 'featured-done.xml').read_text(), APPS['alipay']['package'])
        self.assertFalse(before.goodies_state()['all_claimed'])
        self.assertTrue(after.goodies_state()['all_claimed'])
        self.assertEqual([s['amount'] for s in after.goodies_state()['stages']], [500, 1000])

    def test_three_rounds_reenter_only_after_progress_increases(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            runner.return_list = Mock(side_effect=[farm_page(n) for n in (1, 2, 3)])
            final = runner.execute_featured(farm_page())
            self.assertEqual(final.task_rows()[0]['current'], 3)
            self.assertEqual([(r['before'], r['after']) for r in runner.results], [(0, 1), (1, 2), (2, 3)])
            self.assertEqual(runner.adb.tap.call_count, 3)

    def test_task_progress_can_advance_multiple_steps_in_one_visit(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            runner.return_list = Mock(return_value=farm_page(3))
            runner.execute_featured(farm_page())
            self.assertEqual(len(runner.results), 1)
            self.assertEqual(runner.results[0]['after'], 3)
            runner.adb.tap.assert_called_once()

    def test_no_progress_is_not_reentered_or_marked_complete(self):
        with tempfile.TemporaryDirectory() as directory, patch('farm.time.sleep'):
            runner = self.runner(directory)
            runner.return_list = Mock(return_value=farm_page())
            runner.perceive = Mock(return_value=farm_page())
            with self.assertRaisesRegex(RuntimeError, '进度未增加'):
                runner.execute_featured(farm_page())
            self.assertEqual(runner.results, [])
            runner.adb.tap.assert_called_once()

    def test_only_final_round_can_use_confirmed_removal(self):
        for before in (0, 2):
            with self.subTest(before=before), tempfile.TemporaryDirectory() as directory, patch('farm.time.sleep'):
                runner = self.runner(directory)
                page = farm_page()
                page.root[0].remove(page.root[0][-1])
                absent = Page(ET.tostring(page.root, encoding='unicode'), APPS['alipay']['package'])
                runner.return_list = Mock(return_value=absent)
                runner.perceive = Mock(return_value=absent)
                if before == 0:
                    with self.assertRaisesRegex(RuntimeError, '未到最后一次却消失'):
                        runner.execute_featured(farm_page(before))
                    self.assertEqual(runner.results, [])
                else:
                    runner.execute_featured(farm_page(before))
                    self.assertEqual(runner.results[0]['after'], 3)

    def test_completed_task_is_not_entered_again(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            runner.execute_featured(farm_page(3, button='已完成'))
            runner.adb.tap.assert_not_called()
            runner.browse_goodies.assert_not_called()

    def test_final_claim_button_uses_verified_claim_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            final = farm_page(3, button='领取')
            runner.return_list = Mock(return_value=final)
            runner.claim_alipay = Mock(return_value=final)
            runner.execute_featured(farm_page(2))
            self.assertEqual(runner.claim_alipay.call_args.args[1], 'featured')

    def test_unavailable_featured_does_not_block_next_task(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            page = farm_page()
            runner.execute_goodies = Mock(return_value=page)
            runner.execute_featured = Mock(side_effect=TaskUnavailable('入口不可定位', page))
            runner.execute_instant = Mock(return_value=page)
            runner.execute_insurance = Mock(return_value=page)
            runner.execute_saving = Mock(return_value=page)
            runner.execute(page)
            runner.execute_instant.assert_not_called()

