"""慢UI采集期间的支付宝滑动节奏及坐标刷新回归测试。"""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from farm import ADB, Runner
from test_goodies import fixture
from test_goodies_cards import cards_page


class BrowseCadenceTests(unittest.TestCase):
    def test_slow_capture_keeps_swiping_until_new_state_arrives(self):
        """UI读取尚未返回时必须继续滑动，不能按采集耗时长时间停留。"""
        release = threading.Event()
        adb = ADB(app="alipay")
        adb.run = Mock(return_value="topResumedActivity=ActivityRecord{ com.eg.android.AlipayGphone/.H5Activity }")
        adb.swipe = Mock(side_effect=lambda page: release.set())
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(adb, Path(directory), 10, app="alipay")
            done = fixture("first")
            def slow_read():
                """只有新一次滑动发生，模拟采集才返回。"""
                if not release.wait(1):
                    raise AssertionError("慢采集期间没有继续滑动")
                return done
            runner.perceive = Mock(side_effect=slow_read)
            with patch("farm.BROWSE_READ_INTERVAL", 0.01):
                self.assertIs(runner.perceive_active_browse(fixture("entry")), done)
            adb.swipe.assert_called_once()
            self.assertTrue(done.browse_motion_since_capture)

    def test_fast_capture_adds_no_unnecessary_swipe(self):
        """UI及时返回时不额外滑动，保留捕获坐标的新鲜性。"""
        with tempfile.TemporaryDirectory() as directory:
            adb = ADB(app="alipay")
            adb.swipe = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            done = fixture("first")
            runner.perceive = Mock(return_value=done)
            self.assertIs(runner.perceive_active_browse(fixture("entry")), done)
            adb.swipe.assert_not_called()
            self.assertFalse(done.browse_motion_since_capture)

    def test_switching_apps_stops_gestures_and_joins_capture(self):
        """前台变为其他应用时停止操作，采集结束后才退出，不能遗留后台线程。"""
        release = threading.Event()
        adb = ADB(app="alipay")
        def foreground(*args, **kwargs):
            """模拟用户切换应用，同时让未完成的只读采集退出。"""
            release.set()
            return ("topResumedActivity=ActivityRecord{ com.taobao.qianniu/.MainActivity }\n"
                    "mResumedActivity=ActivityRecord{ com.eg.android.AlipayGphone/.H5Activity }")
        adb.run = Mock(side_effect=foreground)
        adb.swipe = Mock()
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(adb, Path(directory), 10, app="alipay")
            runner.perceive = Mock(side_effect=lambda: (release.wait(1), fixture("first"))[1])
            with patch("farm.BROWSE_READ_INTERVAL", 0.01):
                with self.assertRaisesRegex(RuntimeError, "前台"):
                    runner.perceive_active_browse(fixture("entry"))
            adb.swipe.assert_not_called()
            self.assertTrue(release.is_set())

    def test_capture_failure_is_propagated_without_more_gestures(self):
        """采集失败时停止浏览，不能留后台循环继续滑动。"""
        with tempfile.TemporaryDirectory() as directory:
            adb = ADB(app="alipay")
            adb.swipe = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            runner.perceive = Mock(side_effect=RuntimeError("采集失败"))
            with self.assertRaisesRegex(RuntimeError, "采集失败"):
                runner.perceive_active_browse(fixture("entry"))
            adb.swipe.assert_not_called()

    def test_unverified_capture_time_limit_stops_and_waits_for_reader(self):
        """不能长时间依据旧页面持续滑动，超时退出前须等待只读采集结束。"""
        ended = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            adb = ADB(app="alipay")
            adb.swipe = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            def slow_read():
                """模拟最终可退出的采集，确保没有遗留工作线程。"""
                ended.wait(0.05)
                ended.set()
                return fixture("first")
            runner.perceive = Mock(side_effect=slow_read)
            with patch("farm.BROWSE_READ_INTERVAL", 0.01), patch("farm.MAX_ACTIVE_READ_SECONDS", 0.001):
                with self.assertRaisesRegex(RuntimeError, "采集过慢"):
                    runner.perceive_active_browse(fixture("entry"))
            self.assertTrue(ended.is_set())
            adb.swipe.assert_not_called()

    @patch("farm.time.sleep")
    def test_card_coordinates_are_refreshed_after_capture_time_swipes(self, sleep):
        """采集期间滚动过后，只能点击重新同步读取到的卡片节点。"""
        with tempfile.TemporaryDirectory() as directory:
            stale, fresh, claimed = cards_page(), cards_page(), cards_page(1, 1)
            stale.browse_motion_since_capture = True
            old_button = stale.goodies_cards()[0]["button"]
            new_button = fresh.goodies_cards()[0]["button"]
            runner = Runner(Mock(), Path(directory), 90, app="alipay")
            runner.perceive = Mock(side_effect=[fresh] + [claimed] * 20)
            _, details = runner.browse_goodies(stale)
            self.assertEqual(details["card_claims"], 1)
            self.assertIs(runner.adb.tap.call_args.args[0], new_button)
            self.assertIsNot(runner.adb.tap.call_args.args[0], old_button)


if __name__ == "__main__":
    unittest.main()
