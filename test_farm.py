import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from farm import ADB, APPS, Page, Runner


def make_page(name="看严选推荐商品", current=3, total=6):
    return Page(f'''<hierarchy><node package="com.taobao.taobao" bounds="[0,0][1260,2800]">
      <node text="肥料明细"/><node><node><node text="{name}({current}/{total})"
      bounds="[200,1500][800,1580]"/></node><node text="去完成" clickable="true"
      bounds="[950,1500][1200,1600]"/></node>
      <node><node text="买限时折扣好物得奖励(0/1)"/>
      <node text="去完成" clickable="true" bounds="[950,1000][1200,1100]"/></node>
      </node></hierarchy>''')


class FarmTests(unittest.TestCase):
    def insurance_page(self, stage):
        return Page((Path(__file__).parent / "tests/fixtures" / f"insurance-{stage}.xml")
                    .read_text(), APPS["alipay"]["package"])

    def test_insurance_real_search_and_reward_signals(self):
        entry = self.insurance_page("entry")
        self.assertTrue(entry.insurance_search)
        self.assertFalse(entry.insurance_done)
        self.assertTrue(self.insurance_page("browsed").insurance_done)
        for stage, current, button in (("before", 0, "去完成"), ("ready", 1, "领取")):
            row = self.insurance_page(stage).task_rows()[0]
            self.assertEqual((row["current"], row["button"].get("text")), (current, button))
        self.assertEqual(self.insurance_page("claimed").task_rows(), [])

    def test_insurance_flow_waits_for_award_and_claims_without_swiping(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"):
            adb = Mock()
            adb.page.side_effect = [self.insurance_page(s) for s in
                                   ("entry", "browsed", "ready", "claimed", "confirm")]
            runner = Runner(adb, Path(directory), 10, app="alipay")
            runner.execute(self.insurance_page("before"))
            self.assertEqual(len(runner.results), 1)
            result = runner.results[0]
            self.assertEqual(result["key"], "insurance")
            self.assertEqual(result["button_after"], "任务已移除")
            self.assertEqual(result["browse_signals"], ["肥料奖励", "已发放"])
            self.assertEqual(adb.tap.call_count, 2)  # 去完成和领取，不点击保险产品。
            adb.back.assert_called_once()
            adb.swipe.assert_not_called()

    def test_insurance_ready_claims_without_revisiting_search(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"):
            adb = Mock()
            adb.page.side_effect = [self.insurance_page("claimed"), self.insurance_page("confirm")]
            runner = Runner(adb, Path(directory), 10, app="alipay")
            runner.execute(self.insurance_page("ready"))
            adb.tap.assert_called_once()
            adb.back.assert_not_called()
            self.assertEqual(runner.results[0]["before"], 1)

    def test_insurance_missing_task_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            runner.execute(self.insurance_page("claimed"))
            adb.tap.assert_not_called()
            self.assertEqual(runner.results, [])

    def test_insurance_timeout_does_not_claim_or_log_success(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"), \
                patch("farm.time.monotonic", side_effect=[0, 11]):
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            runner.wait_page = Mock(return_value=self.insurance_page("entry"))
            with self.assertRaisesRegex(RuntimeError, "浏览超时"):
                runner.execute(self.insurance_page("before"))
            adb.tap.assert_called_once()
            adb.back.assert_not_called()
            self.assertEqual(runner.results, [])

    def test_insurance_task_must_disappear_consecutively(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"):
            adb = Mock()
            adb.page.side_effect = [self.insurance_page(s) for s in ("claimed", "ready", "confirm")]
            runner = Runner(adb, Path(directory), 10, app="alipay")
            with self.assertRaisesRegex(RuntimeError, "领取状态未确认"):
                runner.execute(self.insurance_page("ready"))
            self.assertEqual(runner.results, [])

    def saving_page(self, stage):
        return Page((Path(__file__).parent / "tests/fixtures" / f"saving-{stage}.xml")
                    .read_text(), APPS["alipay"]["package"])

    def test_saving_task_real_before_ready_claimed(self):
        for stage, progress, button in (("before", 0, "去完成"), ("ready", 1, "领取"),
                                        ("claimed", 1, "已领取")):
            row = self.saving_page(stage).task_rows()[0]
            self.assertEqual(row["current"], progress)
            self.assertEqual(row["button"].get("text"), button)

    def test_saving_products_exclude_clicked_partial_and_next_campaign(self):
        page = self.saving_page("venue2")
        products = page.saving_products()
        self.assertEqual(len(products), 2)
        self.assertNotIn("专享优惠券", products[0].get("content-desc"))
        self.assertEqual(page.saving_progress(), (2, 3))
        self.assertTrue(self.saving_page("venue3").saving_done)
        next_campaign = self.saving_page("next-campaign")
        self.assertIsNone(next_campaign.saving_progress())
        self.assertTrue(next_campaign.saving_done)
        self.assertEqual(next_campaign.saving_products(), [])

    def test_saving_complete_flow_verifies_clicks_then_claim(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"):
            adb = Mock()
            adb.page.side_effect = [self.saving_page(s) for s in
                                   ("venue0", "detail", "venue1", "detail", "venue2",
                                    "redirect", "venue3", "ready", "claimed")]
            runner = Runner(adb, Path(directory), 10, app="alipay")
            runner.execute(self.saving_page("before"))
            self.assertEqual(len(runner.results), 1)
            result = runner.results[0]
            self.assertEqual(result["button_after"], "已领取")
            self.assertEqual([r["after"] for r in result["product_clicks"]], [1, 2, 3])
            labels = [r["product"] for r in result["product_clicks"]]
            self.assertEqual(len(set(labels)), 3)
            self.assertEqual(adb.tap.call_count, 5)  # 入口 + 三个商品 + 领取。

    def test_saving_claim_ready_task_without_reentering_venue(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            adb.page.return_value = self.saving_page("claimed")
            runner = Runner(adb, Path(directory), 10, app="alipay")
            runner.execute(self.saving_page("ready"))
            adb.tap.assert_called_once()
            self.assertEqual(runner.results[0]["product_clicks"], [])

    def test_saving_already_claimed_is_not_clicked(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            runner = Runner(adb, Path(directory), 10, app="alipay")
            runner.execute(self.saving_page("claimed"))
            adb.tap.assert_not_called()
            self.assertEqual(runner.results, [])

    def test_saving_unchanged_click_progress_times_out_without_success(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"), \
                patch("farm.time.monotonic", side_effect=[0, 0, 11]):
            adb = Mock()
            adb.page.return_value = self.saving_page("venue0")
            runner = Runner(adb, Path(directory), 10, app="alipay")
            with self.assertRaisesRegex(RuntimeError, "进度未增加"):
                runner.saving_update(0)
            self.assertEqual(runner.results, [])

    def test_saving_unconfirmed_claim_never_counts_success(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"):
            adb = Mock()
            adb.page.return_value = self.saving_page("ready")
            runner = Runner(adb, Path(directory), 10, app="alipay")
            with self.assertRaisesRegex(RuntimeError, "领取状态未确认"):
                runner.execute(self.saving_page("ready"))
            adb.tap.assert_called_once()
            self.assertEqual(runner.results, [])

    def test_saving_cancels_only_known_cross_app_prompt(self):
        prompt = self.saving_page("open-taobao")
        self.assertIsNotNone(prompt.cancel_taobao_prompt())
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            adb.page.side_effect = [prompt, self.saving_page("venue3")]
            runner = Runner(adb, Path(directory), 10, app="alipay")
            self.assertTrue(runner.saving_observe().saving_done)
            self.assertEqual(adb.tap.call_args.args[0].get("content-desc"), "取消")

    def test_alipay_navigation_with_real_ui_labels_stops_at_list(self):
        package = APPS["alipay"]["package"]
        pages = [Page((Path(__file__).parent / "tests/fixtures" / f"alipay-{stage}.xml")
                      .read_text(), package) for stage in ("home", "farm", "tasks")]
        self.assertFalse(pages[1].task_list)  # 首页“任务列表”按钮不等于弹窗已打开。
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            adb.running.side_effect = [True, False]
            adb.page.side_effect = pages
            runner = Runner(adb, Path(directory), 10, app="alipay")
            page = runner.bootstrap()
            self.assertTrue(page.task_list)
            self.assertEqual(adb.tap.call_args_list[0].args[0].get("text"), "芭芭农场")
            self.assertEqual(adb.tap.call_args_list[1].args[0].get("text"), "任务列表")
            runner.execute(page)
            self.assertEqual(adb.tap.call_count, 2)
            adb.swipe.assert_not_called()
            self.assertEqual(runner.results, [])
            self.assertEqual(runner.summary()["app"], "alipay")

    def test_alipay_controls_only_selected_package(self):
        adb = ADB(app="alipay")
        adb.run = Mock(return_value="NAME\ncom.taobao.taobao\n")
        self.assertFalse(adb.running())
        adb.run.return_value = "NAME\ncom.eg.android.AlipayGphone:push\n"
        self.assertTrue(adb.running())
        adb.stop()
        adb.run.assert_called_with("shell", "am", "force-stop", APPS["alipay"]["package"])

    def test_ui_coordinates_scale_to_actual_input_size(self):
        adb = ADB(app="alipay")
        adb.run = Mock(return_value="Physical size: 1260x2800\n")
        self.assertEqual(adb.input_point(539, 1500, (1080, 2400)), (628, 1750))
        adb.run.return_value = "Physical size: 1260x2800\nOverride size: 1080x2400\n"
        self.assertEqual(adb.input_point(539, 1500, (1080, 2400)), (539, 1500))

    def test_scaled_tap_uses_latest_page_viewport(self):
        adb = ADB(app="alipay")
        adb.run = Mock(return_value="Physical size: 1260x2800\n")
        adb.viewport = (1080, 2400)
        node = Page('<hierarchy><node text="芭芭农场" bounds="[467,1479][611,1521]"/></hierarchy>').nodes[0]
        adb.tap(node)
        adb.run.assert_called_with("shell", "input", "tap", "628", "1750")

    def test_process_detection_includes_taobao_child_but_not_other_apps(self):
        adb = ADB()
        adb.run = Mock(return_value="NAME\ncom.taobao.qianniu\ncom.taobao.taobao:channel\n")
        self.assertTrue(adb.running())
        adb.run.return_value = "NAME\ncom.taobao.qianniu\ncom.taobao.taobao.fake\n"
        self.assertFalse(adb.running())

    def test_navigation_stops_running_app_and_uses_accessibility_label(self):
        home = Page('''<hierarchy><node package="com.taobao.taobao" bounds="[0,0][1260,2800]">
            <node package="com.taobao.taobao" content-desc="芭芭农场" clickable="false"
            bounds="[466,398][671,613]"/></node></hierarchy>''')
        farm = Page('''<hierarchy><node package="com.taobao.taobao" bounds="[0,0][1260,2800]">
            <node package="com.taobao.taobao" text="集肥料" clickable="true"
            bounds="[853,1957][1036,2149]"/></node></hierarchy>''')
        tasks = make_page()
        for running in (True, False):
            with self.subTest(running=running), tempfile.TemporaryDirectory() as directory:
                adb = Mock()
                adb.running.side_effect = [True, False] if running else [False]
                adb.page.side_effect = [home, farm, tasks]
                runner = Runner(adb, Path(directory), 10)
                result = runner.bootstrap()
                self.assertIs(result, tasks)
                calls = [call[0] for call in adb.mock_calls]
                self.assertEqual(calls, (["running", "stop", "running"] if running else ["running"])
                                 + ["launch", "page", "tap", "page", "tap", "page"])
                self.assertEqual(adb.tap.call_args_list[0].args[0].get("content-desc"), "芭芭农场")
                self.assertEqual(adb.tap.call_args_list[1].args[0].get("text"), "集肥料")

    def test_failed_stop_never_launches(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            adb.running.return_value = True
            with self.assertRaisesRegex(RuntimeError, "仍存在"):
                Runner(adb, Path(directory), 10).bootstrap()
            adb.launch.assert_not_called()
            adb.tap.assert_not_called()

    def test_entry_rejects_invisible_or_wrong_app(self):
        page = Page('''<hierarchy><node package="com.taobao.taobao" bounds="[0,0][1260,2800]">
            <node package="com.taobao.taobao" content-desc="芭芭农场" bounds="[0,0][0,0]"/>
            <node package="another.app" text="芭芭农场" bounds="[10,10][100,100]"/>
            </node></hierarchy>''')
        self.assertIsNone(page.entry("芭芭农场"))

    def gift_page(self, stage):
        path = Path(__file__).parent / "tests" / "fixtures" / f"gift-{stage}.xml"
        return Page(path.read_text(encoding="utf-8"))

    def test_gift_real_device_before_after(self):
        before = self.gift_page("before").task_rows()[0]
        after = self.gift_page("after").task_rows()[0]
        self.assertEqual((before["current"], before["button"].get("text")), (0, "去领取"))
        self.assertEqual((after["current"], after["button"].get("text")), (1, "20点可领"))
        self.assertEqual(after["button"].get("clickable"), "true")

    def test_gift_claim_logs_button_transition_and_does_not_back(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            runner = Runner(adb, Path(directory), 10)
            before = self.gift_page("before")
            runner.perceive = Mock(return_value=self.gift_page("after"))
            runner.claim_gift(before, before.task_rows()[0])
            adb.tap.assert_called_once()
            adb.back.assert_not_called()
            adb.swipe.assert_not_called()
            self.assertEqual(runner.results[0]["button_before"], "去领取")
            self.assertEqual(runner.results[0]["button_after"], "20点可领")
            self.assertEqual(runner.summary()["gift_claims"], 1)

    def test_gift_future_time_not_clicked_even_if_progress_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            runner = Runner(adb, Path(directory), 10)
            xml = (Path(__file__).parent / "tests/fixtures/gift-after.xml").read_text()
            page = Page(xml.replace("礼包(1/1)", "礼包(0/1)"))
            runner.claim_gift(page, page.task_rows()[0])
            adb.tap.assert_not_called()
            self.assertEqual(runner.results, [])

    def test_gift_completed_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            runner = Runner(adb, Path(directory), 10)
            runner.execute(self.gift_page("after"))
            adb.tap.assert_not_called()
            self.assertEqual(runner.results, [])

    def test_alias_and_button_belong_to_same_row(self):
        for name in ("看严选推荐商品", "发现精选好物"):
            page = make_page(name)
            self.assertTrue(page.task_list)
            rows = page.task_rows()
            self.assertEqual(len(rows), 1)  # 购买任务不能混入。
            self.assertEqual(rows[0]["key"], "selected")
            self.assertEqual(rows[0]["button"].get("bounds"), "[950,1500][1200,1600]")

    def test_completion_is_positive_text_not_missing_countdown(self):
        for text, expected in (("当前页下单", "done"), ("任务完成 +150", "done"),
                               ("滑动浏览得肥料", "browsing"), ("", "unknown"),
                               ("搜索有福利", "search")):
            self.assertEqual(Page(f'<hierarchy><node text="{text}"/></hierarchy>')
                             .browse_state(), expected)

    def test_failed_dump_never_reads_old_xml(self):
        adb = ADB()
        adb.run = Mock(side_effect=["ERROR: could not get idle state.", ""])
        with self.assertRaises(RuntimeError):
            adb.page()
        self.assertFalse(any("cat" in call.args for call in adb.run.call_args_list))

    def test_no_progress_does_not_log_success(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            runner = Runner(adb, Path(directory), 10)
            page = make_page()
            runner.perceive = Mock(return_value=page)
            runner.browse = Mock()
            runner.return_list = Mock(return_value=page)
            with self.assertRaisesRegex(RuntimeError, "次数未增加"):
                runner.execute(page)
            self.assertEqual(runner.results, [])

    def test_final_round_disappearance_is_verified_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            runner = Runner(adb, Path(directory), 10)
            after = Page('''<hierarchy><node package="com.taobao.taobao"
                bounds="[0,0][1260,2800]"><node text="肥料明细"/>
                <node text="去完成"/><node text="400肥料到账啦!"/></node></hierarchy>''')
            runner.perceive = Mock(side_effect=[after, after])
            runner.browse = Mock()
            runner.return_list = Mock(return_value=after)
            runner.execute(make_page(current=5))
            self.assertEqual(runner.results[0]["after"], 6)
            self.assertEqual(runner.results[0]["reward_messages"], ["400肥料到账啦!"])


if __name__ == "__main__":
    unittest.main()
