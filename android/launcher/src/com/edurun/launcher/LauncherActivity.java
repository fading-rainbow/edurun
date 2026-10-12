package com.edurun.launcher;

import android.app.Activity;
import android.content.Intent;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Bundle;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.json.JSONArray;
import org.json.JSONObject;

/** 配对到本机后台。没有QQ密码、人脸识别、图片上传或伪造跑步结果的代码。 */
public final class LauncherActivity extends Activity {
    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private TextView status, checks, photo;
    private Button start;
    private boolean alive = true;
    private boolean busy;

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    private TextView text(String value, int size, boolean bold) {
        TextView view = new TextView(this);
        view.setText(value); view.setTextSize(size); view.setTextColor(Color.rgb(25, 39, 56));
        if (bold) view.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        view.setPadding(0, dp(8), 0, dp(8));
        return view;
    }

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(Color.rgb(242, 246, 250));
        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(dp(22), dp(24), dp(22), dp(24));
        scroll.addView(content);
        content.addView(text("乐跑自动助手", 26, true));
        content.addView(text("先核对准备条件，再开始自动流程", 14, false));
        status = text("实验版 · 尚未通过整体验收", 17, true); content.addView(status);
        checks = text("待检查：\n• 乐跑QQ／微信授权\n• 本人照片\n• 场地与打卡点\n• 设备与运动输入", 15, false);
        content.addView(checks);
        photo = text(photoReady() ? "本人照片：已选择（尚未验证身份）" : "本人照片：未选择", 14, false);
        content.addView(photo);
        Button choose = new Button(this); choose.setText("选择本人照片");
        choose.setOnClickListener(v -> {
            if (busy) return;
            Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            intent.setType("image/*"); intent.addCategory(Intent.CATEGORY_OPENABLE);
            startActivityForResult(intent, 10);
        });
        content.addView(choose);
        start = new Button(this); start.setText("一键检查并开始");
        start.setOnClickListener(v -> request("/v1/start")); content.addView(start);
        Button inspect = new Button(this); inspect.setText("仅检查准备条件");
        inspect.setOnClickListener(v -> request("/v1/check")); content.addView(inspect);
        content.addView(text("仅连接这台电脑的本机后台；账号密码不在助手中输入。\n"
            + "准备检查失败不会启动乐跑。本人验证等必要操作出现时暂停等待。", 12, false));
        setContentView(scroll);
    }

    private boolean photoReady() {
        File file = new File(getFilesDir(), "portrait.image");
        if (!file.isFile() || file.length() == 0 || file.length() > 15 * 1024 * 1024) return false;
        BitmapFactory.Options options = new BitmapFactory.Options(); options.inJustDecodeBounds = true;
        BitmapFactory.decodeFile(file.getAbsolutePath(), options);
        return options.outWidth >= 128 && options.outHeight >= 128;
    }

    private static byte[] readLimited(InputStream input, int maximum) throws Exception {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream(); byte[] buffer = new byte[8192];
        int size;
        while ((size = input.read(buffer)) != -1) {
            if (bytes.size() + size > maximum) throw new IllegalArgumentException();
            bytes.write(buffer, 0, size);
        }
        return bytes.toByteArray();
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request != 10 || result != RESULT_OK || data == null || data.getData() == null) return;
        File temporary = new File(getFilesDir(), "portrait.tmp");
        try (InputStream input = getContentResolver().openInputStream(data.getData())) {
            byte[] bytes = readLimited(input, 15 * 1024 * 1024);
            BitmapFactory.Options options = new BitmapFactory.Options(); options.inJustDecodeBounds = true;
            BitmapFactory.decodeByteArray(bytes, 0, bytes.length, options);
            if (options.outWidth < 128 || options.outHeight < 128) throw new IllegalArgumentException();
            try (FileOutputStream output = new FileOutputStream(temporary)) { output.write(bytes); }
            if (!temporary.renameTo(new File(getFilesDir(), "portrait.image"))) throw new IllegalStateException();
            photo.setText("本人照片：已选择（原文件保留，不代表身份验证通过）");
        } catch (Exception error) {
            status.setText("照片选择失败；未替换已保存照片");
        } finally { temporary.delete(); }
    }

    private void request(String path) {
        if (busy) return;
        busy = true;
        start.setEnabled(false); status.setText("正在核对准备条件…");
        final boolean ready = photoReady();
        io.execute(() -> {
            HttpURLConnection connection = null;
            String message;
            JSONObject response = null;
            try {
                File config = new File(getFilesDir(), "launcher.json");
                JSONObject paired;
                try (InputStream input = new FileInputStream(config)) {
                    paired = new JSONObject(new String(readLimited(input, 16384), StandardCharsets.UTF_8));
                }
                String token = paired.getString("token");
                if (!token.matches("[A-Za-z0-9_-]{40,100}")) throw new IllegalArgumentException();
                // 固定loopback；经本项目adb reverse进入本机127.0.0.1，不连接中转站或公网。
                connection = (HttpURLConnection) new URL("http://127.0.0.1:8766" + path).openConnection();
                connection.setConnectTimeout(3000); connection.setReadTimeout(20000);
                connection.setRequestMethod("POST"); connection.setDoOutput(true);
                connection.setInstanceFollowRedirects(false);
                connection.setRequestProperty("X-Launcher-Token", token);
                connection.setRequestProperty("Content-Type", "application/json");
                byte[] body = new JSONObject().put("photo_ready", ready).toString().getBytes(StandardCharsets.UTF_8);
                connection.setFixedLengthStreamingMode(body.length);
                connection.getOutputStream().write(body);
                int code = connection.getResponseCode();
                try (InputStream input = code < 400 ? connection.getInputStream() : connection.getErrorStream()) {
                    response = new JSONObject(new String(readLimited(input, 32768), StandardCharsets.UTF_8));
                }
                message = response.optString("message", "没有收到明确准备结果");
            } catch (Exception error) {
                message = "后台未连接或未完成配对；未开始乐跑";
            } finally { if (connection != null) connection.disconnect(); }
            final String result = message; final JSONObject report = response;
            runOnUiThread(() -> {
                if (!alive) return;
                busy = false; status.setText(result); start.setEnabled(true);
                if (report != null) {
                    JSONArray items = report.optJSONArray("checks"); StringBuilder lines = new StringBuilder();
                    if (items != null) for (int i = 0; i < items.length(); i++) {
                        JSONObject item = items.optJSONObject(i); if (item == null) continue;
                        String marker = "pass".equals(item.optString("state")) ? "✓ " : "○ ";
                        lines.append(marker).append(item.optString("title")).append("\n")
                            .append(item.optString("detail")).append("\n\n");
                    }
                    checks.setText(lines.toString());
                }
            });
        });
    }

    @Override protected void onDestroy() {
        alive = false; io.shutdownNow(); super.onDestroy();
    }
}
