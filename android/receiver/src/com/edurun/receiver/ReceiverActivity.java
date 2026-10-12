package com.edurun.receiver;

import android.app.Activity;
import android.content.Intent;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.widget.TextView;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import org.json.JSONArray;
import org.json.JSONObject;

/** 独立UID的只读接收器；不注入、不联网、不计算或伪造步数。 */
public final class ReceiverActivity extends Activity implements SensorEventListener {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final JSONArray events = new JSONArray();
    private SensorManager manager;
    private String request;
    private boolean registered, captureDone;
    private TextView display;
    private long started;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state); request = getIntent().getStringExtra("receiver_request");
        if (request == null || !request.matches("[a-f0-9]{32}")) { finish(); return; }
        display = new TextView(this); display.setTextSize(18); display.setPadding(24,24,24,24);
        setContentView(display); manager = (SensorManager) getSystemService(SENSOR_SERVICE);
        started = SystemClock.elapsedRealtimeNanos();
        Sensor acc = manager.getDefaultSensor(1), gyro = manager.getDefaultSensor(4);
        registered = acc != null && gyro != null && manager.registerListener(this, acc, 20000)
            && manager.registerListener(this, gyro, 20000);
        handler.post(snapshot);
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (request != null && request.equals(intent.getStringExtra("receiver_request"))
            && intent.getBooleanExtra("capture_done", false)) {
            captureDone = true; manager.unregisterListener(this); writeSnapshot();
        }
    }

    @Override public void onSensorChanged(SensorEvent event) {
        if (captureDone || events.length() >= 8192 || (event.sensor.getType()!=1 && event.sensor.getType()!=4)) return;
        try {
            JSONObject item = new JSONObject(); item.put("type", event.sensor.getType());
            item.put("timestamp_ns", event.timestamp); JSONArray values = new JSONArray();
            for (float value : event.values) values.put(value); item.put("values", values); events.put(item);
        } catch (Exception ignored) {}
    }
    @Override public void onAccuracyChanged(Sensor sensor, int accuracy) {}

    private final Runnable snapshot = new Runnable() {
        @Override public void run() {
            writeSnapshot(); if (!captureDone) handler.postDelayed(this, 250);
        }
    };

    private void writeSnapshot() {
        try {
            JSONObject result = new JSONObject(); result.put("package", getPackageName());
            result.put("request_id", request); result.put("started_ns", started);
            result.put("uid", android.os.Process.myUid());
            result.put("elapsed_realtime_ns", SystemClock.elapsedRealtimeNanos());
            result.put("registered", registered); result.put("event_count", events.length());
            result.put("capture_done", captureDone);
            if (captureDone) result.put("received_samples", events);
            File temporary = new File(getFilesDir(), "receiver-state.tmp");
            try (FileOutputStream stream = new FileOutputStream(temporary)) {
                stream.write(result.toString().getBytes(StandardCharsets.UTF_8));
            }
            temporary.renameTo(new File(getFilesDir(), "receiver-state.json"));
            display.setText("独立只读IMU接收检验\n已注册："+registered+"\n事件："+events.length()
                +"\n结束采集："+captureDone+"\n不产生步数、位置或跑步记录");
        } catch (Exception ignored) {}
    }

    @Override public void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        if (manager != null) manager.unregisterListener(this); super.onDestroy();
    }
}
