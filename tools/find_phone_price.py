import subprocess, time, re, xml.etree.ElementTree as ET
# 持续读取无障碍标签，发现目标售价后立即停止滚动。
p = subprocess.Popen(["adb", "shell", "CLASSPATH=/data/local/tmp/bbnc-farmdump4.dex:/system/framework/uiautomator.jar app_process /system/bin FarmDump --watch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
try:
    while "ready" not in p.stdout.readline():
        pass
    previous = None
    repeats = 0
    for i in range(1000):
        path = "/sdcard/price-scan.xml"
        p.stdin.write(path + "\n")
        p.stdin.flush()
        while "dumped to:" not in p.stdout.readline():
            pass
        root = ET.fromstring(subprocess.check_output(["adb", "shell", "cat", path]))
        visible = []
        for node in root.iter("node"):
            text = node.get("text", "")
            coords = list(map(int, re.findall(r"-?\d+", node.get("bounds", ""))))
            if "优惠后" in text and len(coords) == 4 and coords[3] > 550 and coords[1] < 2737:
                visible.append(text)
                if (price := re.search(r"优惠后([0-9.]+)元", text)) and float(price.group(1)) <= 0.03:
                    print("FOUND " + text + " " + node.get("bounds"), flush=True)
                    raise SystemExit
        repeats = repeats + 1 if visible == previous else 0
        previous = visible
        if repeats >= 4:
            print("END: list stopped changing", flush=True)
            break
        print(str(i) + " " + " | ".join(re.findall(r"优惠后[^，]+", " | ".join(visible))), flush=True)
        subprocess.run(["adb", "shell", "input", "swipe", "650", "2400", "650", "1000", "550"], check=True)
        time.sleep(0.25)
finally:
    p.stdin.close()
    p.terminate()
