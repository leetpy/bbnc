import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.UiAutomation;
import android.graphics.Rect;
import android.util.Xml;
import android.view.accessibility.AccessibilityNodeInfo;
import java.io.BufferedReader;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import org.xmlpull.v1.XmlSerializer;

/** 只读 UI 采集器：维持无障碍连接，跳过动画页面的 idle 等待。 */
public class FarmDump {
    private static void node(XmlSerializer out, AccessibilityNodeInfo current) throws Exception {
        out.startTag(null, "node");
        String[] keys = {"text", "content-desc", "package", "resource-id", "class"};
        CharSequence[] values = {
            current.getText(), current.getContentDescription(), current.getPackageName(),
            current.getViewIdResourceName(), current.getClassName()
        };
        for (int i = 0; i < keys.length; i++) {
            out.attribute(null, keys[i], values[i] == null ? "" : values[i].toString());
        }
        Rect rect = new Rect();
        current.getBoundsInScreen(rect);
        out.attribute(null, "bounds", "[" + rect.left + "," + rect.top + "]["
            + rect.right + "," + rect.bottom + "]");
        out.attribute(null, "clickable", String.valueOf(current.isClickable()));
        out.attribute(null, "enabled", String.valueOf(current.isEnabled()));
        for (int i = 0; i < current.getChildCount(); i++) {
            AccessibilityNodeInfo child = current.getChild(i);
            if (child != null) node(out, child);
        }
        out.endTag(null, "node");
    }

    private static void dump(UiAutomation ui, String path) throws Exception {
        // 某些系统支持显式清空缓存；其余系统依赖持续连接中的页面事件更新。
        try {
            UiAutomation.class.getMethod("clearCache").invoke(ui);
        } catch (ReflectiveOperationException ignored) {
            // 兼容没有此方法的旧版系统。
        }
        AccessibilityNodeInfo root = ui.getRootInActiveWindow();
        if (root == null) throw new IllegalStateException("Missing accessibility root");
        try (FileOutputStream stream = new FileOutputStream(path)) {
            XmlSerializer out = Xml.newSerializer();
            out.setOutput(stream, "UTF-8");
            out.startDocument("UTF-8", true);
            out.startTag(null, "hierarchy");
            node(out, root);
            out.endTag(null, "hierarchy");
            out.endDocument();
        }
        // 完整 XML 写入成功才确认，Python 收到确认后才读取该唯一文件。
        System.out.println("dumped to: " + path);
        System.out.flush();
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 1) throw new IllegalArgumentException("Expected path or --watch");
        Class<?> cls = Class.forName("com.android.uiautomator.core.UiAutomationShellWrapper");
        Object wrapper = cls.getDeclaredConstructor().newInstance();
        cls.getMethod("connect").invoke(wrapper);
        try {
            UiAutomation ui = (UiAutomation) cls.getMethod("getUiAutomation").invoke(wrapper);
            AccessibilityServiceInfo info = ui.getServiceInfo();
            info.flags |= AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS
                | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS;
            ui.setServiceInfo(info);
            // 给 WebView 时间响应无障碍连接；协议等待和采集均有 Python 端超时。
            Thread.sleep(2000);
            System.out.println("ready");
            System.out.flush();
            if (!args[0].equals("--watch")) {
                dump(ui, args[0]);
                return;
            }
            BufferedReader input = new BufferedReader(new InputStreamReader(System.in));
            String path;
            while ((path = input.readLine()) != null) dump(ui, path);
        } finally {
            // stdin 关闭或采集异常时释放无障碍连接，不影响当前应用页面。
            cls.getMethod("disconnect").invoke(wrapper);
        }
    }
}
