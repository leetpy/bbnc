"""现身卡片回归：实机结构、滚动后新增、逐卡核验与10张上限。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from farm import APPS, Page, Runner
from test_goodies import fixture


def cards_page(total=1, claimed=0):
    """基于实机卡片结构生成已结束计时的列表；新卡片按列表末尾追加。"""
    page = fixture('cards-before')
    page.exact('滑动浏览15秒得肥料').set('text', '已获得肥料')
    template = ET.tostring(page.root[0][-1], encoding='unicode')
    page.root[0].remove(page.root[0][-1])
    for index in range(total):
        card = ET.fromstring(template)
        if index < claimed:
            card[0].set('text', '已得肥料100')
            card[1].set('text', '已领取')
        page.root[0].append(card)
    return Page(ET.tostring(page.root, encoding='unicode'), APPS['alipay']['package'])


class GoodiesCardsTests(unittest.TestCase):
    def test_real_card_before_and_after_have_same_identity(self):
        before, after = fixture('cards-before'), fixture('cards-after')
        self.assertEqual(before.goodies_state()['layout'], 'single')
        first, second = before.goodies_cards()[0], after.goodies_cards()[0]
        self.assertEqual(first['id'], second['id'])
        self.assertFalse(first['claimed'])
        self.assertTrue(second['claimed'])
        self.assertEqual(second['amount'], 100)

    def test_unrelated_claim_buttons_are_ignored(self):
        page = cards_page()
        ad = ET.SubElement(page.root[0], 'node')
        ET.SubElement(ad, 'node', text='商品推荐')
        ET.SubElement(ad, 'node', text='立即领')
        page = Page(ET.tostring(page.root, encoding='unicode'), APPS['alipay']['package'])
        self.assertEqual(len(page.goodies_cards()), 1)
        self.assertEqual(Page(ET.tostring(page.root, encoding='unicode')).goodies_cards(), [])

    def test_new_card_after_swipe_is_claimed_after_browse_already_done(self):
        with tempfile.TemporaryDirectory() as directory, patch('farm.time.sleep'):
            adb = Mock()
            runner = Runner(adb, Path(directory), 90, app='alipay')
            final = cards_page(2, 2)
            snapshots = iter([cards_page(1, 1), cards_page(2, 1), final])
            runner.perceive = Mock(side_effect=lambda: next(snapshots, final))
            _, details = runner.browse_goodies(cards_page())
            self.assertEqual(adb.tap.call_count, 2)
            self.assertEqual(details['card_claims'], 2)
            self.assertEqual(details['card_reward_amount'], 200)
            self.assertEqual(details['cards_stop_reason'], '连续滑动未发现新卡片')

    def test_ten_card_limit_includes_previously_claimed_cards(self):
        for already in (0, 8, 10):
            with self.subTest(already=already), tempfile.TemporaryDirectory() as directory, patch('farm.time.sleep'):
                adb = Mock()
                count = [already]
                adb.tap.side_effect = lambda node: count.__setitem__(0, count[0] + 1)
                runner = Runner(adb, Path(directory), 90, app='alipay')
                runner.perceive = Mock(side_effect=lambda: cards_page(11, count[0]))
                _, details = runner.browse_goodies(cards_page(11, already))
                self.assertEqual(adb.tap.call_count, 10 - already)
                self.assertEqual(details['cards_claimed_observed'], 10)
                self.assertEqual(details['card_reward_amount'], (10 - already) * 100)

    def test_failed_click_is_not_retried_or_counted(self):
        with tempfile.TemporaryDirectory() as directory, patch('farm.time.sleep'):
            adb = Mock()
            runner = Runner(adb, Path(directory), 90, app='alipay')
            runner.perceive = Mock(return_value=cards_page())
            with self.assertRaisesRegex(RuntimeError, '未确认100肥料已领取'):
                runner.browse_goodies(cards_page())
            adb.tap.assert_called_once()
            self.assertEqual(runner.results, [])

    def test_offscreen_card_is_not_clicked(self):
        page = cards_page()
        card = page.goodies_cards()[0]
        card['button'].set('bounds', '[228,0][504,-300]')
        with tempfile.TemporaryDirectory() as directory, patch('farm.time.sleep'):
            adb = Mock()
            runner = Runner(adb, Path(directory), 90, app='alipay')
            runner.perceive = Mock(return_value=page)
            _, details = runner.browse_goodies(page)
            adb.tap.assert_not_called()
            self.assertEqual(details['card_reward_amount'], 0)

    def test_stale_fixed_header_blocks_positive_but_unreliable_card_coordinates(self):
        # 实机固定导航被 XML 报到屏外时，卡片的正坐标也可能指向别的商品。
        page = cards_page()
        page.exact('浏览得奖励').set('bounds', '[90,0][324,-1000]')
        self.assertFalse(page.goodies_cards()[0]['coordinates_valid'])
        with tempfile.TemporaryDirectory() as directory, patch('farm.time.sleep'):
            adb = Mock()
            runner = Runner(adb, Path(directory), 90, app='alipay')
            runner.perceive = Mock(return_value=page)
            runner.browse_goodies(page)
            adb.tap.assert_not_called()

    def test_card_scan_time_limit_preserves_verified_browse_completion(self):
        with tempfile.TemporaryDirectory() as directory, patch('farm.time.sleep'), \
                patch('farm.time.monotonic', side_effect=[0, 0, 0, 100]):
            runner = Runner(Mock(), Path(directory), 90, app='alipay')
            page = cards_page(1, 1)
            runner.perceive = Mock(return_value=page)
            _, details = runner.browse_goodies(page)
            self.assertEqual(details['cards_stop_reason'], '达到扫描时间上限')
            self.assertEqual(details['card_reward_amount'], 0)

    def test_new_cards_are_not_discarded_when_initial_browse_is_done(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 90, app='alipay')
            runner.wait_page = Mock(return_value=cards_page(1, 1))
            details = {'new_rewards': [], 'new_card_rewards': [{'amount': 100}], 'card_reward_amount': 100}
            runner.browse_goodies = Mock(return_value=(cards_page(1, 1), details))
            runner.return_list = Mock(return_value=fixture('continue'))
            runner.execute_goodies(fixture('continue'))
            self.assertEqual(runner.results[0]['card_reward_amount'], 100)


if __name__ == '__main__':
    unittest.main()
