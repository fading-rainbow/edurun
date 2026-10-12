package com.edurun.diagnostic;

import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.SystemClock;
import java.io.File;
import java.io.FileInputStream;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import org.json.JSONArray;
import org.json.JSONObject;

/** 独立诊断：只读自有CSV、最多10秒，不循环、不补帧、不生成步数或目标App结果。 */
final class RawMotionProbe implements SensorEventListener {
    private final SensorManager sensors;
    private final File input;
    private final boolean stallTest;
    private final String requestId;
    private final Object lock = new Object();
    private final ArrayList<float[]> rows = new ArrayList<>();
    private final JSONArray sent = new JSONArray(), received = new JSONArray();
    private final JSONObject report = new JSONObject();
    private HandlerThread thread;
    private Handler worker;
    private Method init, send;
    private Sensor acceleration, gyroscope;
    private boolean active;
    private int index;
    private long firstSendNs, lastSendNs, maximumGapNs;

    RawMotionProbe(SensorManager manager, File file, boolean stall, String request) {
        sensors = manager; input = file; stallTest = stall; requestId = request;
    }

    private void load() throws Exception {
        if (!input.isFile() || input.length() == 0 || input.length() > 128 * 1024) {
            throw new IllegalArgumentException();
        }
        byte[] data = new byte[(int) input.length()];
        try (FileInputStream stream = new FileInputStream(input)) {
            int total = 0, count;
            while (total < data.length && (count = stream.read(data, total, data.length-total)) > 0) {
                total += count;
            }
            if (total != data.length || stream.read() != -1) throw new IllegalArgumentException();
        }
        String[] lines = new String(data, StandardCharsets.UTF_8).split("\\r?\\n");
        if (!lines[0].equals("t_s,ax,ay,az,gx,gy,gz") || lines.length < 52 || lines.length > 502) {
            throw new IllegalArgumentException();
        }
        for (int i = 1; i < lines.length; i++) {
            String[] columns = lines[i].split(",", -1);
            if (columns.length != 7) throw new IllegalArgumentException();
            float[] row = new float[7];
            for (int j = 0; j < 7; j++) {
                row[j] = Float.parseFloat(columns[j]);
                if (!Float.isFinite(row[j]) || (j > 0 && Math.abs(row[j]) > 100)) {
                    throw new IllegalArgumentException();
                }
            }
            if (Math.abs(row[0] - (i-1) * 0.02) > 0.00001) throw new IllegalArgumentException();
            rows.add(row);
        }
        float duration = rows.get(rows.size()-1)[0];
        if (duration < 1 || duration > 10) throw new IllegalArgumentException();
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(data);
        StringBuilder hash = new StringBuilder();
        for (byte value : digest) hash.append(String.format("%02x", value & 255));
        report.put("clip_sha256", hash.toString());
        report.put("declared_duration_s", duration);
        report.put("source_rows", rows.size());
        report.put("nominal_hz", 50);
        report.put("stall_test", stallTest);
    }

    void start() {
        synchronized (lock) {
            try {
                report.put("diagnostic_only", true); report.put("state", "initializing");
                if (requestId == null || !requestId.matches("[a-f0-9]{32}")) {
                    throw new IllegalArgumentException();
                }
                report.put("request_id", requestId);
                load();
                acceleration = sensors.getDefaultSensor(Sensor.TYPE_ACCELEROMETER);
                gyroscope = sensors.getDefaultSensor(Sensor.TYPE_GYROSCOPE);
                if (acceleration == null || gyroscope == null) throw new IllegalArgumentException();
                init = SensorManager.class.getMethod("initDataInjection", boolean.class);
                send = SensorManager.class.getMethod("injectSensorData", Sensor.class,
                    float[].class, int.class, long.class);
                active = Boolean.TRUE.equals(init.invoke(sensors, true));
                report.put("initialized", active);
                if (!active) { report.put("state", "not_supported"); return; }
                if (!sensors.registerListener(this, acceleration, 20000)
                    || !sensors.registerListener(this, gyroscope, 20000)) {
                    finish("receiver_registration_failed"); return;
                }
                thread = new HandlerThread("edurun-raw-motion-diagnostic"); thread.start();
                worker = new Handler(thread.getLooper());
                report.put("state", "running"); worker.post(frame);
            } catch (Exception failure) { finish("probe_rejected"); }
        }
    }

    private final Runnable frame = new Runnable() {
        @Override public void run() {
            synchronized (lock) {
                if (!active) return;
                try {
                    long now = SystemClock.elapsedRealtimeNanos();
                    if (lastSendNs > 0) {
                        long gap = now-lastSendNs;
                        maximumGapNs = Math.max(maximumGapNs, gap);
                        if (gap > 100_000_000L) { finish("clock_gap"); return; }
                        if (gap < 19_000_000L) {
                            worker.postDelayed(this, (19_000_000L-gap+999_999L)/1_000_000L);
                            return;
                        }
                    }
                    if (firstSendNs == 0) firstSendNs = now;
                    float[] row = rows.get(index);
                    Sensor[] channels = {acceleration, gyroscope};
                    for (int channel = 0; channel < 2; channel++) {
                        int offset = 1 + 3*channel;
                        float[] values = {row[offset], row[offset+1], row[offset+2]};
                        long timestamp = SystemClock.elapsedRealtimeNanos();
                        if (!Boolean.TRUE.equals(send.invoke(sensors, channels[channel], values,
                            SensorManager.SENSOR_STATUS_ACCURACY_HIGH, timestamp))) {
                            finish("send_rejected"); return;
                        }
                        JSONObject sample = sample(channels[channel].getType(), timestamp, values);
                        sample.put("source_index", index); sample.put("source_t_s", row[0]);
                        sent.put(sample);
                    }
                    lastSendNs = now; index++;
                    if (index == rows.size()) {
                        worker.postDelayed(() -> finish("completed"), 200);
                    } else {
                        // 以本次实际发送时间为基准，不追赶积压，不循环片段。
                        long due = now + 20_000_000L;
                        long delay = Math.max(1, (due-SystemClock.elapsedRealtimeNanos()+999_999L)/1_000_000L);
                        if (stallTest && index == 25) delay = 250;
                        worker.postDelayed(this, delay);
                    }
                } catch (Exception failure) { finish("probe_rejected"); }
            }
        }
    };

    private static JSONObject sample(int type, long timestamp, float[] values) throws Exception {
        JSONObject item = new JSONObject(); item.put("type", type); item.put("timestamp_ns", timestamp);
        JSONArray vector = new JSONArray(); for (float value : values) vector.put(value);
        item.put("values", vector); return item;
    }

    @Override public void onSensorChanged(SensorEvent event) {
        synchronized (lock) {
            if (!active || received.length() >= 4096) return;
            if (event.sensor.getType() != 1 && event.sensor.getType() != 4) return;
            try { received.put(sample(event.sensor.getType(), event.timestamp, event.values)); }
            catch (Exception ignored) {}
        }
    }
    @Override public void onAccuracyChanged(Sensor sensor, int accuracy) {}

    private void finish(String state) {
        synchronized (lock) {
            try {
                report.put("state", state);
                if (active && init != null) report.put("client_closed", Boolean.TRUE.equals(init.invoke(sensors, false)));
            } catch (Exception failure) {
                try { report.put("client_closed", false); } catch (Exception ignored) {}
            } finally {
                active = false; sensors.unregisterListener(this);
                if (worker != null) worker.removeCallbacksAndMessages(null);
                if (thread != null) thread.quitSafely();
            }
        }
    }

    JSONObject snapshot() {
        synchronized (lock) {
            try {
                report.put("sent_rows", index); report.put("first_send_ns", firstSendNs);
                report.put("last_send_ns", lastSendNs); report.put("maximum_gap_ns", maximumGapNs);
                report.put("sent_sample_count", sent.length()); report.put("received_sample_count", received.length());
                // 回放中不序列化全量原始事件，避免主线程快照/大文本布局阻塞接收队列。
                // 完成或失败后再输出完整证据；运行中的计数不作为接收验收证明。
                if (!active) { report.put("sent_samples", sent); report.put("received_samples", received); }
                // 脱离工作线程中的可变对象，快照序列化不会遍历并发写入的JSONArray。
                return new JSONObject(report.toString());
            } catch (Exception failure) { return new JSONObject(); }
        }
    }

    void close() {
        synchronized (lock) { if (active) finish("activity_destroyed"); }
    }
}
