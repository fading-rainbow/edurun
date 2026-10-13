package com.edurun.diagnostic;

import android.app.Service;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Intent;
import android.hardware.SensorManager;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Bundle;
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
    private long preparedUntilNs;
    private LocationManager locations;
    private boolean gpsRegistered;
    private Location gpsFix;
    private long gpsCallbacks, firstGpsReceivedNs, lastGpsReceivedNs, taskStartedNs;
    private long gpsGuardRejections;
    // 接收回调的本机单调时钟与Location内的时钟分别保存；不修正来源或伪造新定位。
    private final LocationListener gpsListener = new LocationListener() {
        @Override public void onLocationChanged(Location location) {
            long now = SystemClock.elapsedRealtimeNanos();
            if (!"gps".equals(location.getProvider()) || taskStartedNs == 0 || now < taskStartedNs) return;
            if (gpsCallbacks++ == 0) firstGpsReceivedNs = now;
            lastGpsReceivedNs = now; gpsFix = new Location(location);
        }
        @Override public void onStatusChanged(String provider, int status, Bundle extras) {}
        @Override public void onProviderEnabled(String provider) {}
        @Override public void onProviderDisabled(String provider) {}
    };

    @Override public void onCreate() {
        super.onCreate();
        NotificationManager notifications = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        notifications.createNotificationChannel(new NotificationChannel("imu-diagnostic",
            "本机运动输入诊断", NotificationManager.IMPORTANCE_LOW));
        startForeground(51, new Notification.Builder(this, "imu-diagnostic")
            .setSmallIcon(android.R.drawable.ic_menu_info_details)
            .setContentTitle("本机有界IMU诊断")
            .setContentText("两秒授权超时停止；不生成步数或跑步记录").build());
        locations = (LocationManager) getSystemService(LOCATION_SERVICE);
        try {
            locations.requestLocationUpdates(LocationManager.GPS_PROVIDER, 0, 0, gpsListener);
            gpsRegistered = true;
        } catch (SecurityException | IllegalArgumentException denied) {
            gpsRegistered = false; // 无权限/无provider不能冒充接收；不申请或改权限。
        }
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) { stopSelf(); return START_NOT_STICKY; }
        String request = intent.getStringExtra("motion_probe_request");
        long observed = intent.getLongExtra("motion_observed_ns", 0);
        int ttl = intent.getIntExtra("motion_lease_ms", 0);
        if ("com.edurun.diagnostic.MOTION_RENEW".equals(intent.getAction())) {
            if (probe != null) {
                if (!intent.getBooleanExtra("motion_require_received_gps", false)
                        || receivedGpsMatches(request, intent)) probe.renewLease(request, observed, ttl);
                else gpsGuardRejections++;
            }
            else stopSelf();
            return START_NOT_STICKY;
        }
        if ("com.edurun.diagnostic.MOTION_STOP".equals(intent.getAction())) {
            if (probe != null && requestId.equals(request)) probe.cancel();
            if (probe == null) stopSelf();
            return START_NOT_STICKY;
        }
        boolean prepare = "com.edurun.diagnostic.MOTION_PREPARE".equals(intent.getAction());
        boolean start = "com.edurun.diagnostic.MOTION_START".equals(intent.getAction());
        if (start && probe != null && requestId.equals(request) && probe.isPrepared()) {
            if (SystemClock.elapsedRealtimeNanos() >= preparedUntilNs) probe.cancel();
            else { probe.requireLease(observed, ttl); probe.start(); }
            return START_NOT_STICKY;
        }
        if ((!start && !prepare)
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
        taskStartedNs = SystemClock.elapsedRealtimeNanos();
        probe = new RawMotionProbe((SensorManager) getSystemService(SENSOR_SERVICE),
            new File(getFilesDir(), "motion-probe.csv"), false, request,
            intent.getBooleanExtra("motion_bounded_stream", false),
            intent.getIntExtra("motion_replay_passes", 1));
        if (prepare) {
            probe.prepare(); preparedUntilNs = SystemClock.elapsedRealtimeNanos() + 30_000_000_000L;
        } else { probe.requireLease(observed, ttl); probe.start(); }
        handler.post(snapshot);
        return START_NOT_STICKY;
    }

    private final Runnable snapshot = new Runnable() {
        @Override public void run() {
            if (probe == null) return;
            if (probe.isPrepared() && SystemClock.elapsedRealtimeNanos() >= preparedUntilNs) probe.cancel();
            JSONObject result = probe.snapshot();
            try {
                JSONObject output = new JSONObject(); output.put("package", getPackageName());
                output.put("uid", android.os.Process.myUid());
                output.put("request_id", requestId);
                output.put("elapsed_realtime_ns", SystemClock.elapsedRealtimeNanos());
                output.put("raw_motion_probe", result);
                JSONObject gps = new JSONObject();
                gps.put("registered", gpsRegistered);
                gps.put("task_started_ns", taskStartedNs);
                gps.put("callback_count", gpsCallbacks);
                gps.put("first_received_ns", firstGpsReceivedNs);
                gps.put("last_received_ns", lastGpsReceivedNs);
                gps.put("guard_rejections", gpsGuardRejections);
                if (gpsFix != null) {
                    gps.put("provider", gpsFix.getProvider());
                    gps.put("latitude", gpsFix.getLatitude());
                    gps.put("longitude", gpsFix.getLongitude());
                    gps.put("has_accuracy", gpsFix.hasAccuracy());
                    gps.put("accuracy_m", gpsFix.getAccuracy());
                    gps.put("location_elapsed_ns", gpsFix.getElapsedRealtimeNanos());
                    gps.put("location_time_ms", gpsFix.getTime());
                    gps.put("is_mock", gpsFix.isFromMockProvider());
                }
                output.put("gps_reception", gps);
                File temporary = new File(getFilesDir(), "motion-service.tmp");
                try (FileOutputStream stream = new FileOutputStream(temporary)) {
                    stream.write(output.toString().getBytes(StandardCharsets.UTF_8));
                }
                if (!temporary.renameTo(new File(getFilesDir(), "motion-service.json"))) {
                    probe.close(); stopSelf(); return;
                }
            } catch (Exception failure) { probe.close(); stopSelf(); return; }
            String state = result.optString("state");
            if (!state.equals("running") && !state.equals("initializing") && !state.equals("prepared")) stopSelf();
            else handler.postDelayed(this, 250);
        }
    };

    private boolean receivedGpsMatches(String request, Intent intent) {
        long now = SystemClock.elapsedRealtimeNanos();
        if (requestId == null || !requestId.equals(request) || !gpsRegistered || gpsFix == null
                || gpsCallbacks < 2 || firstGpsReceivedNs < taskStartedNs
                || lastGpsReceivedNs <= firstGpsReceivedNs || now < lastGpsReceivedNs
                || now - lastGpsReceivedNs > 2_000_000_000L
                || !gpsFix.hasAccuracy() || !Float.isFinite(gpsFix.getAccuracy())
                || gpsFix.getAccuracy() < 0 || gpsFix.getAccuracy() > 5
                || !intent.hasExtra("motion_expected_lat") || !intent.hasExtra("motion_expected_lon")) return false;
        float lat = intent.getFloatExtra("motion_expected_lat", Float.NaN);
        float lon = intent.getFloatExtra("motion_expected_lon", Float.NaN);
        if (!Float.isFinite(lat) || !Float.isFinite(lon) || lat < -85 || lat > 85 || lon < -180 || lon > 180) return false;
        Location expected = new Location("gps"); expected.setLatitude(lat); expected.setLongitude(lon);
        return gpsFix.distanceTo(expected) <= 10;
    }

    @Override public IBinder onBind(Intent intent) { return null; }
    @Override public void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        if (probe != null) probe.close();
        if (locations != null) {
            try { locations.removeUpdates(gpsListener); }
            catch (SecurityException revoked) { /* 权限被撤销也不跳过服务清理。 */ }
        }
        stopForeground(true); super.onDestroy();
    }
}
