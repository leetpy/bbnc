import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from farm import ADB, APPS, Page, Runner, TaskUnavailable, ensure_xml_runtime, find_adb


def make_page(name="看严选推荐商品", current=3, total=6):
    return Page(f'''<hierarchy><node package="com.taobao.taobao" bounds="[0,0][1260,2800]">
      <node text="肥料明细"/><node><node><node text="{name}({current}/{total})"
      bounds="[200,1500][800,1580]"/></node><node text="去完成" clickable="true"
      bounds="[950,1500][1200,1600]"/></node>
      <node><node text="买限时折扣好物得奖励(0/1)"/>
      <node text="去完成" clickable="true" bounds="[950,1000][1200,1100]"/></node>
      </node></hierarchy>''')


class FarmTests(unittest.TestCase):
    def test_healthy_xml_runtime_does_not_switch_interpreter(self):
        with patch("farm.subprocess.run") as probe, patch("farm.os.execv") as execute:
            ensure_xml_runtime()
            probe.assert_not_called()
            execute.assert_not_called()

    def test_broken_xml_runtime_switches_only_after_successful_probe(self):
        with patch("farm.ET.fromstring", side_effect=ImportError("expat")), \
                patch("farm.shutil.which", return_value=None), \
                patch("farm.Path.is_file", return_value=True), \
                patch("farm.sys.executable", "/broken/python"), \
                patch("farm.sys.argv", ["farm.py", "--app", "taobao", "--from-current"]), \
                patch("farm.subprocess.run") as probe, patch("farm.os.execv") as execute:
            probe.return_value.returncode = 0
            ensure_xml_runtime()
            probe.assert_called_once()
            self.assertEqual(execute.call_args.args[0], "/usr/bin/python3")
            self.assertEqual(execute.call_args.args[1][2:], ["--app", "taobao", "--from-current"])

    def test_broken_xml_runtime_reports_failure_before_device_actions(self):
        with patch("farm.ET.fromstring", side_effect=ImportError("expat")), \
                patch("farm.shutil.which", return_value=None), \
                patch("farm.Path.is_file", return_value=False), \
                patch("farm.os.execv") as execute:
            with self.assertRaisesRegex(RuntimeError, "XML/expat"):
                ensure_xml_runtime()
            execute.assert_not_called()

    def test_adb_path_configuration_overrides_path(self):
        with patch.dict(os.environ, {"ADB_PATH": "/custom/adb"}, clear=True), \
                patch("farm.shutil.which", return_value="/custom/adb") as which:
            self.assertEqual(find_adb(), "/custom/adb")
            which.assert_called_once_with("/custom/adb")

    def test_adb_uses_path_before_sdk_fallback(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("farm.shutil.which", return_value="/bin/adb"), \
                patch("farm.Path.is_file") as exists:
            self.assertEqual(find_adb(), "/bin/adb")
            exists.assert_not_called()

    def test_adb_finds_sdk_without_path_entry(self):
        for env, expected in (({"ANDROID_HOME": "/sdk"}, "/sdk/platform-tools/adb"),
                              ({"ANDROID_SDK_ROOT": "/sdk"}, "/sdk/platform-tools/adb"),
                              ({}, "/home/test/Library/Android/sdk/platform-tools/adb")):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), \
                    patch("farm.os.name", "posix"), patch("farm.shutil.which", return_value=None), \
                    patch("farm.Path.home", return_value=Path("/home/test")), \
                    patch("farm.Path.is_file", return_value=True), \
                    patch("farm.os.access", return_value=True):
                self.assertEqual(find_adb(), expected)

    def test_missing_adb_reports_configuration_hint(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("farm.shutil.which", return_value=None), \
                patch("farm.Path.is_file", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "ADB_PATH"):
                find_adb()

    def test_invalid_explicit_adb_does_not_silently_fallback(self):
        with patch.dict(os.environ, {"ADB_PATH": "/missing/adb"}, clear=True), \
                patch("farm.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "ADB_PATH"):
                find_adb()

    def test_adb_resolves_once_and_preserves_device_selection(self):
        adb = ADB("device-1")
        with patch("farm.find_adb", return_value="/sdk/adb") as resolve, \
                patch("farm.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = b"device\n"
            adb.connect()
            adb.run("get-state")
            resolve.assert_called_once()
            self.assertEqual(run.call_args.args[0], ["/sdk/adb", "-s", "device-1", "get-state"])

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

    def test_alipay_unlocatable_task_does_not_prevent_next_task(self):
        with tempfile.TemporaryDirectory() as directory:
            page = self.insurance_page("before")
            runner = Runner(Mock(), Path(directory), 10, app="alipay")
            runner.execute_insurance = Mock(side_effect=TaskUnavailable("任务不可定位", page))
            runner.execute_saving = Mock(return_value=page)
            self.assertIs(runner.execute(page), page)
            runner.execute_saving.assert_called_once_with(page)
            self.assertEqual(runner.results, [])

    def test_actual_browse_failure_is_not_mistaken_for_missing_task(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Mock(), Path(directory), 10, app="alipay")
            runner.execute_insurance = Mock(side_effect=RuntimeError("浏览超时"))
            runner.execute_saving = Mock()
            with self.assertRaisesRegex(RuntimeError, "浏览超时"):
                runner.execute(self.insurance_page("before"))
            runner.execute_saving.assert_not_called()

    def test_taobao_unavailable_row_is_skipped_without_retrying_it(self):
        with tempfile.TemporaryDirectory() as directory:
            page = make_page()
            runner = Runner(Mock(), Path(directory), 10)
            runner.reveal = Mock(side_effect=TaskUnavailable("任务不在当前列表", page))
            runner.execute(page)
            runner.reveal.assert_called_once()
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

    def test_offscreen_carousel_does_not_expand_screen_or_shift_taps(self):
        page = Page('<hierarchy><node bounds="[0,0][1260,2800]" package="com.taobao.taobao">'
                    '<node text="集肥料" package="com.taobao.taobao" bounds="[853,1957][1036,2149]"/>'
                    '<node text="轮播屏外元素" bounds="[40000,2224][41640,3206]"/>'
                    '</node></hierarchy>')
        self.assertEqual((page.width, page.height), (1260, 2800))
        self.assertFalse(page.visible(page.nodes[-1]))
        adb = ADB()
        adb.viewport = (page.width, page.height)
        adb.run = Mock(return_value="Physical size: 1260x2800\n")
        adb.tap(page.entry("集肥料"))
        adb.run.assert_called_with("shell", "input", "tap", "944", "2053")

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

    def test_navigation_establishes_live_collection_before_opening_farm(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = ADB()
            adb.running = Mock(return_value=False)
            adb.launch = Mock()
            adb.tap = Mock()
            home = Page('<hierarchy><node package="com.taobao.taobao" bounds="[0,0][1260,2800]">'
                        '<node package="com.taobao.taobao" text="芭芭农场" bounds="[400,400][600,600]"/>'
                        '</node></hierarchy>')
            farm = Page('<hierarchy><node package="com.taobao.taobao" bounds="[0,0][1260,2800]">'
                        '<node package="com.taobao.taobao" text="集肥料" bounds="[850,1900][1030,2100]"/>'
                        '</node></hierarchy>')
            pages = iter((home, farm, make_page()))
            def read_page():
                self.assertTrue(adb.use_live_ui)
                return next(pages)
            adb.page = Mock(side_effect=read_page)
            self.assertTrue(Runner(adb, Path(directory), 10).bootstrap().task_list)
            self.assertEqual(adb.tap.call_count, 2)

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

    def test_reward_overlay_is_not_a_ready_task_list(self):
        page = Page((Path(__file__).parent / "tests/fixtures/yesterday-reward.xml").read_text())
        self.assertTrue(page.reward_popup)
        self.assertFalse(page.task_list)
        close = page.reward_popup_close()
        self.assertIsNotNone(close)
        self.assertEqual(close.get("bounds"), "[567,1937][695,2062]")
        self.assertNotEqual(close.get("text"), "立即领取")

    def test_wait_closes_known_reward_overlay_without_claiming(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"):
            adb = Mock()
            popup = Page((Path(__file__).parent / "tests/fixtures/yesterday-reward.xml").read_text())
            after = self.gift_page("before")
            adb.page.side_effect = [popup, after]
            runner = Runner(adb, Path(directory), 10)
            self.assertIs(runner.wait_page(lambda p: p.task_list, "任务列表"), after)
            adb.tap.assert_called_once()
            self.assertEqual(adb.tap.call_args.args[0].get("bounds"), "[567,1937][695,2062]")
            adb.swipe.assert_not_called()
            self.assertEqual(runner.results, [])

    def test_reveal_waits_for_list_after_transient_background_snapshot(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"):
            adb = Mock()
            before = self.gift_page("before")
            row = before.task_rows()[0]
            row["button"].set("bounds", "[0,3000][100,3100]")
            # XML 根节点仍是屏幕尺寸，礼包按钮确实位于屏幕外。
            before.height = 2800
            after = self.gift_page("before")
            background = Page('<hierarchy><node package="com.taobao.taobao" '
                              'text="芭芭农场" bounds="[0,0][1260,2800]"/></hierarchy>')
            adb.page.side_effect = [background, after]
            runner = Runner(adb, Path(directory), 10)
            found_page, found = runner.reveal(before, row)
            self.assertIs(found_page, after)
            self.assertEqual(found["key"], "gift")
            adb.swipe.assert_called_once()
            adb.tap.assert_not_called()
            self.assertEqual(adb.page.call_count, 2)

    def test_reveal_does_not_keep_swiping_when_list_is_unconfirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            before = self.gift_page("before")
            row = before.task_rows()[0]
            row["button"].set("bounds", "[0,3000][100,3100]")
            before.height = 2800
            runner = Runner(adb, Path(directory), 10)
            runner.wait_page = Mock(side_effect=RuntimeError("等待列表超时"))
            with self.assertRaisesRegex(RuntimeError, "等待列表超时"):
                runner.reveal(before, row)
            adb.swipe.assert_called_once()
            adb.tap.assert_not_called()
            self.assertEqual(runner.results, [])

    def test_list_wait_timeout_saves_observed_background_xml(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"), \
                patch("farm.time.monotonic", side_effect=[0, 0, 21]):
            adb = Mock()
            adb.page.return_value = Page('<hierarchy><node package="com.taobao.taobao" '
                                         'text="芭芭农场" bounds="[0,0][1260,2800]"/></hierarchy>')
            runner = Runner(adb, Path(directory), 10)
            with self.assertRaisesRegex(RuntimeError, "滚动后的任务列表"):
                runner.wait_page(lambda p: p.task_list, "滚动后的任务列表", timeout=20)
            snapshots = list(Path(directory).glob("wait-timeout-*.xml"))
            self.assertEqual(len(snapshots), 1)
            self.assertFalse(Page(snapshots[0].read_text()).task_list)
            adb.tap.assert_not_called()
            adb.swipe.assert_not_called()

    def test_alias_and_button_belong_to_same_row(self):
        for name in ("看严选推荐商品", "发现精选好物", "搜一搜你心仪的宝贝"):
            page = make_page(name)
            self.assertTrue(page.task_list)
            rows = page.task_rows()
            self.assertEqual(len(rows), 1)  # 购买任务不能混入。
            self.assertEqual(rows[0]["key"], "selected")
            self.assertEqual(rows[0]["button"].get("bounds"), "[950,1500][1200,1600]")

    def search_page(self):
        # 保留实机搜索发现结构和边界，裁去私人历史及无关词条。
        return Page((Path(__file__).parent / "tests/fixtures/search-baby-entry.xml").read_text())

    def test_search_terms_are_scoped_to_verified_discovery(self):
        page = self.search_page()
        self.assertEqual([n.get("content-desc") for n in page.search_terms()],
                         ["卫生纸 近一个月曝光超10万"])
        self.assertEqual(Page('<hierarchy><node text="搜索有福利"/></hierarchy>').search_terms(), [])
        term = page.search_terms()[0]
        term.set("bounds", "[0,0][0,0]")
        self.assertEqual(page.search_terms(), [])

    def test_search_enters_results_then_reuses_browse(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"):
            adb = Mock()
            results = Page('''<hierarchy><node package="com.taobao.taobao" bounds="[0,0][1260,2800]">
                <node text="芭芭农场-固搜-interact"/><node text="滑动浏览得肥料"/>
                </node></hierarchy>''')
            done = Page('''<hierarchy><node package="com.taobao.taobao" bounds="[0,0][1260,2800]">
                <node text="芭芭农场-固搜-interact"/><node text="立即下单最高得肥料+30000"/>
                </node></hierarchy>''')
            adb.page.side_effect = [results, done]
            runner = Runner(adb, Path(directory), 10)
            runner.browse(self.search_page())
            adb.tap.assert_called_once()  # 只点一个搜索词，不点商品或购买文案。
            adb.swipe.assert_called_once_with(results)

    def test_real_search_result_and_completion_signals(self):
        for stage, expected in (("results", "browsing"), ("done", "done")):
            page = Page((Path(__file__).parent / "tests/fixtures"
                         / f"search-baby-{stage}.xml").read_text())
            self.assertTrue(page.search_results)
            self.assertEqual(page.browse_state(), expected)
            self.assertEqual(page.search_terms(), [])

    def test_reader_start_failure_closes_process_without_reading_xml(self):
        adb = ADB()
        adb.run = Mock(return_value="")
        adb.reader_wait = Mock(side_effect=RuntimeError("进程退出"))
        with patch("farm.subprocess.Popen") as process:
            with self.assertRaisesRegex(RuntimeError, "进程退出"):
                adb.live_dump("/sdcard/unique.xml")
            process.return_value.stdin.close.assert_called_once()
            process.return_value.wait.assert_called_once()
        self.assertIsNone(adb.ui_reader)
        self.assertIsNone(adb.ui_remote)
        self.assertFalse(any("cat" in c.args for c in adb.run.call_args_list))

    def test_repeated_search_entry_does_not_click_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            runner = Runner(adb, Path(directory), 10)
            runner.wait_page = Mock(return_value=self.search_page())
            with self.assertRaisesRegex(RuntimeError, "仍停留在入口页"):
                runner.browse(self.search_page())
            adb.tap.assert_called_once()
            self.assertEqual(runner.results, [])

    def test_task_identity_separates_simultaneous_18_and_5_rounds(self):
        page = make_page(current=0, total=18)
        other = make_page("搜一搜你心仪的宝贝", current=2, total=5)
        page.root[0].append(other.root[0][1])
        page = Page(ET.tostring(page.root, encoding="unicode"))
        rows = page.task_rows()
        self.assertEqual(len(rows), 2)
        found = Runner.task_row(page, rows[1])
        self.assertEqual((found["current"], found["total"]), (2, 5))

    def test_search_alias_change_uses_same_task_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            runner = Runner(adb, Path(directory), 10)
            before = make_page("搜一搜你心仪的宝贝", current=0, total=5)
            after = make_page("看严选推荐商品", current=1, total=5)
            runner.perceive = Mock(return_value=after)
            runner.browse = Mock()
            runner.return_list = Mock(return_value=after)
            # 下一轮浏览主动停止，只检查第一轮进度及名称变化不影响核验。
            runner.browse.side_effect = [None, RuntimeError("停止下一轮")]
            with self.assertRaisesRegex(RuntimeError, "停止下一轮"):
                runner.execute(before, only=("selected", 5))
            self.assertEqual(runner.results[0]["after"], 1)

    def test_18_round_task_matches_changed_search_title_and_finishes_all_rounds(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"):
            adb = Mock()
            runner = Runner(adb, Path(directory), 10)
            before = make_page("看严选推荐商品", current=0, total=18)
            runner.browse = Mock()
            runner.perceive = Mock(return_value=before)
            runner.return_list = Mock(side_effect=[
                make_page("搜一搜你喜欢的商品", current=n, total=18) for n in range(1, 19)])
            runner.execute(before, only=("selected", 18))
            self.assertEqual(len(runner.results), 18)
            self.assertEqual(runner.results[0]["after"], 1)
            self.assertEqual(runner.results[0]["task_after"], "搜一搜你喜欢的商品")
            self.assertEqual(runner.results[-1]["after"], 18)
            self.assertEqual(adb.tap.call_count, 18)

    def test_new_task_after_initial_task_can_finish_and_scan_normally(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.MAX_TASK_ROUNDS", 2):
            adb = Mock()
            runner = Runner(adb, Path(directory), 10)
            later = make_page("逛精选好物", current=0, total=1)
            completed = make_page("逛精选好物", current=1, total=1)
            def claim_first(page, row):
                # 首屏只有礼包，完成后才出现浏览任务；不操作任何真实设备。
                runner.results.append({"key": "gift"})
                return later
            runner.claim_gift = Mock(side_effect=claim_first)
            runner.browse = Mock()
            runner.perceive = Mock(return_value=completed)
            runner.return_list = Mock(return_value=completed)
            runner.execute(self.gift_page("before"))
            self.assertEqual(len(runner.results), 2)
            self.assertEqual(runner.summary()["status"], "ok")
            runner.claim_gift.assert_called_once()
            adb.tap.assert_called_once()

    def test_round_cap_still_stops_before_next_pending_task(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.MAX_TASK_ROUNDS", 1):
            adb = Mock()
            runner = Runner(adb, Path(directory), 10)
            runner.browse = Mock()
            runner.perceive = Mock(return_value=make_page(current=1, total=18))
            runner.return_list = Mock(return_value=make_page(current=1, total=18))
            with self.assertRaisesRegex(RuntimeError, "仍有待执行任务"):
                runner.execute(make_page(current=0, total=18))
            self.assertEqual(len(runner.results), 1)
            adb.tap.assert_called_once()

    def test_single_browse_task_can_change_to_search_title(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Mock()
            runner = Runner(adb, Path(directory), 10)
            before = make_page("逛精选好物", current=0, total=1)
            after = make_page("搜一搜你喜欢的商品", current=1, total=1)
            runner.browse = Mock()
            runner.perceive = Mock(return_value=before)
            runner.return_list = Mock(return_value=after)
            runner.execute(before)
            self.assertEqual(runner.results[0]["key"], "browse15")
            self.assertEqual(runner.results[0]["after"], 1)

    def test_unknown_task_title_is_recorded_instead_of_guessing_success(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"):
            adb = Mock()
            runner = Runner(adb, Path(directory), 10)
            before = make_page(current=0, total=18)
            after = make_page("尚未验证的新任务名称", current=1, total=18)
            runner.browse = Mock()
            runner.perceive = Mock(return_value=after)
            runner.return_list = Mock(return_value=after)
            with self.assertRaisesRegex(RuntimeError, "任务身份未匹配"):
                runner.execute(before)
            self.assertEqual(runner.results, [])
            adb.tap.assert_called_once()
            self.assertEqual(len(list(Path(directory).glob("task-unmatched-*.xml"))), 1)

    def test_ambiguous_task_identity_is_not_matched_to_first_row(self):
        page = make_page(current=0, total=18)
        duplicate = make_page("搜一搜你喜欢的商品", current=1, total=18)
        page.root[0].append(duplicate.root[0][1])
        page = Page(ET.tostring(page.root, encoding="unicode"))
        with self.assertRaisesRegex(RuntimeError, "任务身份不唯一"):
            Runner.task_row(page, page.task_rows()[0])

    def test_search_final_removal_ignores_other_same_family_task(self):
        with tempfile.TemporaryDirectory() as directory, patch("farm.time.sleep"):
            adb = Mock()
            runner = Runner(adb, Path(directory), 10)
            before = make_page("搜一搜你心仪的宝贝", current=4, total=5)
            after = make_page("看严选推荐商品", current=0, total=18)
            runner.perceive = Mock(return_value=after)
            runner.browse = Mock()
            runner.return_list = Mock(return_value=after)
            runner.execute(before, only=("selected", 5))
            self.assertEqual(len(runner.results), 1)
            self.assertEqual(runner.results[0]["after"], 5)
            adb.tap.assert_called_once()  # 18 次任务仍在列表，也不会连带点击。

    def test_reader_protocol_keeps_buffered_acknowledgement(self):
        # 用真实管道验证一次读取包含多行时，不丢失下一条确认信息。
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb", buffering=0) as source:
            os.write(write_fd, b"ready\ndumped to: /sdcard/unique.xml\n")
            os.close(write_fd)
            adb = ADB()
            adb.ui_reader = Mock(stdout=source)
            adb.reader_wait("ready", timeout=1)
            adb.reader_wait("dumped to: /sdcard/unique.xml", timeout=1)
            with self.assertRaisesRegex(RuntimeError, "进程已退出"):
                adb.reader_wait("another file", timeout=1)

    def test_live_dump_failure_does_not_read_stale_xml(self):
        adb = ADB()
        adb.use_live_ui = True
        adb.run = Mock(return_value="")
        adb.live_dump = Mock(side_effect=RuntimeError("采集超时"))
        with self.assertRaisesRegex(RuntimeError, "采集超时"):
            adb.page()
        self.assertFalse(any("cat" in c.args for c in adb.run.call_args_list))

    def test_completion_is_positive_text_not_missing_countdown(self):
        for text, expected in (("当前页下单", "done"), ("任务完成 +150", "done"),
                               ("滑动浏览得肥料", "browsing"), ("", "unknown"),
                               ("搜索有福利", "search")):
            self.assertEqual(Page(f'<hierarchy><node text="{text}"/></hierarchy>')
                             .browse_state(), expected)

    def test_failed_dump_never_reads_old_xml(self):
        adb = ADB()
        adb.run = Mock(side_effect=["ERROR: could not get idle state.", ""])
        adb.live_dump = Mock(side_effect=RuntimeError("采集器也失败"))
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
