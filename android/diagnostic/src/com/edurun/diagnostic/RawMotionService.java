package com.edurun.diagnostic;

import android.app.Service;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Intent;
import android.hardware.SensorManager;
import android.os.Handler;
import android.os.Looper;
import android.os.IBinder;
import android.os.SystemClock;
import java.io.File;
import java.io.FileOutputStream;
import java.io.FileInputStream;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;

/** 有界诊断前台服务。只接受受DUMP权限保护的本机命令，过期不能重新激活。 */
public final class RawMotionService extends Service {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private RawMotionProbe probe;
    private String requestId;

    @Override public void onCreate() {
        super.onCreate();
        NotificationManager notifications = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        notifications.createNotificationChannel(new NotificationChannel("imu-diagnostic",
            "本机运动输入诊断", NotificationManager.IMPORTANCE_LOW));
        startForeground(51, new Notification.Builder(this, "imu-diagnostic")
            .setSmallIcon(android.R.drawable.ic_menu_info_details)
            .setContentTitle("有界IMU诊断正在运行")
            .setContentText("两秒授权超时停止；不生成步数或跑步记录").build());
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) { stopSelf(); return START_NOT_STICKY; }
        String request = intent.getStringExtra("motion_probe_request");
        long observed = intent.getLongExtra("motion_observed_ns", 0);
        int ttl = intent.getIntExtra("motion_lease_ms", 0);
        if ("com.edurun.diagnostic.MOTION_RENEW".equals(intent.getAction())) {
            if (probe != null) probe.renewLease(request, observed, ttl);
            else stopSelf();
            return START_NOT_STICKY;
        }
        if ("com.edurun.diagnostic.MOTION_STOP".equals(intent.getAction())) {
            if (probe != null && requestId.equals(request)) probe.cancel();
            if (probe == null) stopSelf();
            return START_NOT_STICKY;
        }
        if (!"com.edurun.diagnostic.MOTION_START".equals(intent.getAction())
            || probe != null || request == null || !request.matches("[a-f0-9]{32}")) {
            if (probe == null) stopSelf();
            return START_NOT_STICKY;
        }
        // 单个最近任务的重复START也不能在服务重建后重新激活；新的正常任务必须新nonce。
        File lastJob = new File(getFilesDir(), "motion-last-request.txt");
        try {
            if (lastJob.isFile()) {
                byte[] bytes = new byte[33]; int count;
                try (FileInputStream stream = new FileInputStream(lastJob)) { count = stream.read(bytes); }
                if (count > 0 && request.equals(new String(bytes, 0, count, StandardCharsets.UTF_8))) {
                    stopSelf(); return START_NOT_STICKY;
                }
            }
            try (FileOutputStream stream = new FileOutputStream(lastJob)) {
                stream.write(request.getBytes(StandardCharsets.UTF_8));
            }
        } catch (Exception failure) { stopSelf(); return START_NOT_STICKY; }
        requestId = request;
        probe = new RawMotionProbe((SensorManager) getSystemService(SENSOR_SERVICE),
            new File(getFilesDir(), "motion-probe.csv"), false, request);
        probe.requireLease(observed, ttl); probe.start(); handler.post(snapshot);
        return START_NOT_STICKY;
    }

    private final Runnable snapshot = new Runnable() {
        @Override public void run() {
            if (probe == null) return;
            JSONObject result = probe.snapshot();
            try {
                JSONObject output = new JSONObject(); output.put("package", getPackageName());
                output.put("uid", android.os.Process.myUid());
                output.put("request_id", requestId);
                output.put("elapsed_realtime_ns", SystemClock.elapsedRealtimeNanos());
                output.put("raw_motion_probe", result);
                File temporary = new File(getFilesDir(), "motion-service.tmp");
                try (FileOutputStream stream = new FileOutputStream(temporary)) {
                    stream.write(output.toString().getBytes(StandardCharsets.UTF_8));
                }
                if (!temporary.renameTo(new File(getFilesDir(), "motion-service.json"))) {
                    probe.close(); stopSelf(); return;
                }
            } catch (Exception failure) { probe.close(); stopSelf(); return; }
            String state = result.optString("state");
            if (!state.equals("running") && !state.equals("initializing")) stopSelf();
            else handler.postDelayed(this, 250);
        }
    };

    @Override public IBinder onBind(Intent intent) { return null; }
    @Override public void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        if (probe != null) probe.close();
        stopForeground(true); super.onDestroy();
    }
}
