package com.edurun.receiver;

import android.app.Service;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Intent;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.os.Handler;
import android.os.Looper;
import android.os.IBinder;
import android.os.SystemClock;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import org.json.JSONArray;
import org.json.JSONObject;

/** 最多15秒的独立UID只读接收服务；保持目标App前台，不注入或生成步数。 */
public final class ReceiverService extends Service implements SensorEventListener {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final JSONArray events = new JSONArray();
    private SensorManager manager;
    private String request;
    private long started, deadline;
    private boolean registered, done;

    @Override public void onCreate() {
        super.onCreate();
        NotificationManager n = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        n.createNotificationChannel(new NotificationChannel("imu-receiver",
            "独立只读IMU接收检验", NotificationManager.IMPORTANCE_LOW));
        startForeground(52, new Notification.Builder(this, "imu-receiver")
            .setSmallIcon(android.R.drawable.ic_menu_info_details)
            .setContentTitle("独立只读接收检验")
            .setContentText("限时停止；不产生步数或跑步记录").build());
    }

    @Override public int onStartCommand(Intent intent, int flags, int id) {
        if (intent == null) { if (request == null) stopSelf(); return START_NOT_STICKY; }
        String nonce = intent.getStringExtra("receiver_request");
        if ("com.edurun.receiver.RECEIVER_STOP".equals(intent.getAction())) {
            if (request != null && request.equals(nonce)) finishCapture();
            else if (request == null) stopSelf();
            return START_NOT_STICKY;
        }
        int seconds = intent.getIntExtra("receiver_seconds", 0);
        if (!"com.edurun.receiver.RECEIVER_START".equals(intent.getAction())
            || request != null || nonce == null || !nonce.matches("[a-f0-9]{32}")
            || seconds < 1 || seconds > 15) {
            if (request == null) stopSelf();
            return START_NOT_STICKY;
        }
        request = nonce; started = SystemClock.elapsedRealtimeNanos();
        deadline = started + seconds * 1000000000L;
        manager = (SensorManager) getSystemService(SENSOR_SERVICE);
        Sensor acc = manager.getDefaultSensor(1), gyro = manager.getDefaultSensor(4);
        registered = acc != null && gyro != null && manager.registerListener(this, acc, 20000)
            && manager.registerListener(this, gyro, 20000);
        if (!registered) finishCapture(); else handler.post(snapshot);
        return START_NOT_STICKY;
    }

    @Override public void onSensorChanged(SensorEvent e) {
        if (done || events.length() >= 8192 || SystemClock.elapsedRealtimeNanos() >= deadline
            || (e.sensor.getType() != 1 && e.sensor.getType() != 4)) return;
        try {
            JSONObject item = new JSONObject(); item.put("type", e.sensor.getType());
            item.put("timestamp_ns", e.timestamp); JSONArray values = new JSONArray();
            for (float value : e.values) values.put(value);
            item.put("values", values); events.put(item);
        } catch (Exception ignored) {}
    }

    @Override public void onAccuracyChanged(Sensor sensor, int accuracy) {}
    @Override public IBinder onBind(Intent intent) { return null; }

    private final Runnable snapshot = new Runnable() {
        @Override public void run() {
            if (SystemClock.elapsedRealtimeNanos() >= deadline) finishCapture();
            else { writeSnapshot(); if (!done) handler.postDelayed(this, 250); }
        }
    };

    private void finishCapture() {
        done = true; handler.removeCallbacksAndMessages(null);
        if (manager != null) manager.unregisterListener(this);
        writeSnapshot(); stopSelf();
    }

    private void writeSnapshot() {
        if (request == null) return;
        try {
            JSONObject value = new JSONObject(); value.put("package", getPackageName());
            value.put("uid", android.os.Process.myUid()); value.put("request_id", request);
            value.put("started_ns", started); value.put("deadline_ns", deadline);
            value.put("elapsed_realtime_ns", SystemClock.elapsedRealtimeNanos());
            value.put("registered", registered); value.put("event_count", events.length());
            value.put("capture_done", done);
            if (done) value.put("received_samples", events);
            File temporary = new File(getFilesDir(), "receiver-service.tmp");
            try (FileOutputStream output = new FileOutputStream(temporary)) {
                output.write(value.toString().getBytes(StandardCharsets.UTF_8));
            }
            if (!temporary.renameTo(new File(getFilesDir(), "receiver-service.json"))) throw new IllegalStateException();
        } catch (Exception error) {
            done = true; if (manager != null) manager.unregisterListener(this); stopSelf();
        }
    }

    @Override public void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        done = true; if (manager != null) manager.unregisterListener(this);
        writeSnapshot(); super.onDestroy();
    }
}
