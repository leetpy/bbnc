"""支付宝单项核验失败隔离及运行范围汇总的回归测试。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from farm import Runner, TaskVerificationFailed
from test_goodies import fixture
from test_instant import fixture as instant_fixture


class RunCompletionTests(unittest.TestCase):
    @patch.object(Runner, "scan_task_page", return_value=None)
    @patch("farm.time.sleep")
    @patch("farm.Page.task_tappable", return_value=True)
    def test_pending_goodies_does_not_block_next_task_after_list_is_refreshed(self, tappable, sleep, scan):
        """复现已领取但农场仍0/1；重读确认列表后必须继续下一条单次任务。"""
        with tempfile.TemporaryDirectory() as directory:
            before = fixture("before")
            runner = Runner(Mock(), Path(directory), 10, app="alipay")
            runner.wait_page = Mock(side_effect=[fixture("entry"), instant_fixture("browsing")])
            runner.browse_goodies = Mock(return_value=(fixture("completed"), {"new_rewards": []}))
            issued = instant_fixture("issued")
            runner.browse_instant = Mock(return_value=(issued, issued.instant_state()))
            runner.return_list = Mock(side_effect=[before, instant_fixture("ready")])
            runner.perceive = Mock(return_value=before)
            runner.execute(before)
            runner.browse_instant.assert_called_once()
            self.assertEqual(runner.adb.tap.call_count, 2)
            summary = runner.summary()
            self.assertEqual(summary["status"], "partial")
            self.assertEqual(summary["failed_tasks"][0]["key"], "goodies")
            self.assertEqual(summary["completed_rounds"], 1)
            self.assertEqual(summary["results"][0]["key"], "instant")
            self.assertFalse(summary["all_supported_tasks_complete"])

    @patch("farm.Page.task_tappable", return_value=True)
    def test_unknown_page_after_verification_failure_still_stops(self, tappable):
        """即使异常属于可恢复类型，也不能凭处理器先前的列表继续点击。"""
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 10, app="alipay")
            runner.execute_goodies = Mock(side_effect=TaskVerificationFailed("进度未更新"))
            runner.execute_instant = Mock()
            runner.perceive = Mock(return_value=fixture("entry"))
            with self.assertRaisesRegex(RuntimeError, "重新确认.*任务列表"):
                runner.execute(fixture("before"))
            runner.execute_instant.assert_not_called()
            runner.adb.tap.assert_not_called()

    def test_direct_single_task_summary_does_not_claim_full_run_complete(self):
        """直接调用单项处理器也须自动标明范围，不把未扫描的空待办当成全部完成。"""
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 10, app="alipay")
            runner.execute_instant(instant_fixture("ready"))
            summary = runner.summary()
            self.assertEqual(summary["run_scope"]["mode"], "single_task")
            self.assertEqual(summary["run_scope"]["task_keys"], ["instant"])
            self.assertEqual(summary["status"], "single_task_ok")
            self.assertFalse(summary["all_supported_tasks_complete"])
            self.assertEqual(summary["scan_coverage"]["status"], "not_scanned")

    def test_verification_summary_has_explicit_scope(self):
        """验证入口明确只验证单项，即使成功也不能显示成整轮成功。"""
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 10, app="alipay")
            runner.set_run_scope("verification", ["instant"])
            runner.execute_instant(instant_fixture("ready"))
            summary = runner.summary()
            self.assertEqual(summary["status"], "verification_ok")
            self.assertFalse(summary["all_supported_tasks_complete"])

    def test_verification_rejects_unrequested_task_before_clicking(self):
        """声明只验证立得任务后，不能再静默执行其他任务却保持原范围。"""
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 10, app="alipay")
            runner.set_run_scope("verification", ["instant"])
            with self.assertRaisesRegex(RuntimeError, "不在声明的运行范围"):
                runner.execute_goodies(fixture("before"))
            runner.adb.tap.assert_not_called()

    @patch.object(Runner, "scan_task_page", return_value=None)
    @patch("farm.Page.task_tappable", return_value=True)
    def test_alipay_only_filter_does_not_execute_other_tasks(self, tappable, scan):
        """显式单项过滤必须传入支付宝调度，不能声明单项却实际执行全部任务。"""
        with tempfile.TemporaryDirectory() as directory:
            page = fixture("before")
            runner = Runner(Mock(), Path(directory), 10, app="alipay")
            runner.execute_goodies = Mock(return_value=page)
            runner.execute_instant = Mock(return_value=page)
            runner.execute(page, only=("instant", 1))
            runner.execute_goodies.assert_not_called()
            runner.execute_instant.assert_called_once()
            self.assertEqual(runner.summary()["run_scope"]["task_keys"], ["instant"])


if __name__ == "__main__":
    unittest.main()
