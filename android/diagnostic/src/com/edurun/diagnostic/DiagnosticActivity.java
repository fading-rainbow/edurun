package com.edurun.diagnostic;

import android.app.Activity;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.widget.ScrollView;
import android.widget.TextView;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;
import org.json.JSONArray;
import org.json.JSONObject;

/** 只记录本App实际收到的事件，不伪造计步、有效跑步或位置来源。 */
public final class DiagnosticActivity extends Activity implements SensorEventListener {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Map<Integer, Metric> metrics = new LinkedHashMap<>();
    private final Map<String, Location> locations = new LinkedHashMap<>();
    private SensorManager sensors;
    private LocationManager manager;
    private TextView display;
    private String error = "";
    private int motionPeriodUs = 40000;
    private final LocationListener listener = new LocationListener() {
        @Override public void onLocationChanged(Location location) {
            locations.put(location.getProvider(), new Location(location));
        }
        @Override public void onStatusChanged(String provider, int status, Bundle extras) {}
        @Override public void onProviderEnabled(String provider) {}
        @Override public void onProviderDisabled(String provider) {}
    };
    private final Runnable snapshot = new Runnable() {
        @Override public void run() {
            writeSnapshot();
            handler.postDelayed(this, 500);
        }
    };

    private static final class Metric {
        final Sensor sensor;
        boolean registered;
        long count, first, last;
        float[] values = new float[0];
        float[] minimum = new float[0], maximum = new float[0];
        Metric(Sensor value) { sensor = value; }
    }

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        display = new TextView(this);
        display.setTextSize(12);
        display.setPadding(16, 16, 16, 16);
        ScrollView scroll = new ScrollView(this);
        scroll.addView(display);
        setContentView(scroll);
        sensors = (SensorManager) getSystemService(SENSOR_SERVICE);
        manager = (LocationManager) getSystemService(LOCATION_SERVICE);
        int requestedPeriod = getIntent().getIntExtra("motion_period_us", 40000);
        if (requestedPeriod >= 20000 && requestedPeriod <= 40000) {
            motionPeriodUs = requestedPeriod;
        }
        for (Sensor sensor : sensors.getSensorList(Sensor.TYPE_ALL)) {
            metrics.put(sensor.getType(), new Metric(sensor));
        }
        int[] requested = {Sensor.TYPE_ACCELEROMETER, Sensor.TYPE_GYROSCOPE,
            Sensor.TYPE_STEP_COUNTER, Sensor.TYPE_STEP_DETECTOR, Sensor.TYPE_GRAVITY,
            Sensor.TYPE_LINEAR_ACCELERATION, Sensor.TYPE_ROTATION_VECTOR};
        for (int type : requested) {
            Metric metric = metrics.get(type);
            if (metric != null) {
                try { metric.registered = sensors.registerListener(this, metric.sensor, motionPeriodUs); }
                catch (SecurityException failure) { error = "sensor permission not granted"; }
            }
        }
        for (String provider : manager.getAllProviders()) {
            if (!provider.equals("gps") && !provider.equals("network") && !provider.equals("fused")) {
                continue;
            }
            try { manager.requestLocationUpdates(provider, 0, 0, listener); }
            catch (SecurityException failure) { error = "location permission not granted"; }
            catch (IllegalArgumentException ignored) { /* 不存在的provider不冒充可用 */ }
        }
        handler.post(snapshot);
    }

    @Override public void onSensorChanged(SensorEvent event) {
        Metric metric = metrics.get(event.sensor.getType());
        if (metric == null) return;
        metric.count++;
        if (metric.first == 0) metric.first = event.timestamp;
        metric.last = event.timestamp;
        metric.values = event.values.clone();
        if (metric.minimum.length == 0) {
            metric.minimum = event.values.clone();
            metric.maximum = event.values.clone();
        } else {
            for (int i = 0; i < event.values.length; i++) {
                metric.minimum[i] = Math.min(metric.minimum[i], event.values[i]);
                metric.maximum[i] = Math.max(metric.maximum[i], event.values[i]);
            }
        }
    }
    @Override public void onAccuracyChanged(Sensor sensor, int accuracy) {}

    @SuppressWarnings("deprecation")
    private static boolean isMockLocation(Location location) {
        // isMock()从API31才存在；API30必须读取旧接口，不能把崩溃当接收成功。
        if (Build.VERSION.SDK_INT >= 31) return location.isMock();
        return location.isFromMockProvider();
    }

    private void writeSnapshot() {
        try {
            JSONObject output = new JSONObject();
            output.put("package", getPackageName());
            output.put("elapsed_realtime_ns", SystemClock.elapsedRealtimeNanos());
            output.put("error", error);
            output.put("requested_motion_period_us", motionPeriodUs);
            JSONArray available = new JSONArray();
            for (Metric metric : metrics.values()) {
                JSONObject item = new JSONObject();
                item.put("type", metric.sensor.getType());
                item.put("name", metric.sensor.getName());
                item.put("registered", metric.registered);
                item.put("event_count", metric.count);
                item.put("last_timestamp_ns", metric.last);
                double hz = metric.last > metric.first
                    ? (metric.count - 1) * 1e9 / (metric.last - metric.first) : 0;
                item.put("received_hz", hz);
                JSONArray values = new JSONArray();
                for (float value : metric.values) values.put(value);
                item.put("values", values);
                JSONArray spans = new JSONArray();
                for (int i = 0; i < metric.minimum.length; i++) {
                    spans.put(metric.maximum[i] - metric.minimum[i]);
                }
                item.put("observed_axis_spans", spans);
                available.put(item);
            }
            output.put("sensors", available);
            JSONArray fixes = new JSONArray();
            for (Location location : locations.values()) {
                JSONObject item = new JSONObject();
                item.put("provider", location.getProvider());
                item.put("latitude", location.getLatitude());
                item.put("longitude", location.getLongitude());
                item.put("speed_mps", location.getSpeed());
                item.put("accuracy_m", location.getAccuracy());
                item.put("is_mock", isMockLocation(location));
                item.put("elapsed_realtime_ns", location.getElapsedRealtimeNanos());
                fixes.put(item);
            }
            output.put("locations", fixes);
            String text = output.toString(2);
            display.setText(text);
            File directory = getFilesDir();
            if (directory != null) {
                File temporary = new File(directory, "diagnostics.json.tmp");
                File destination = new File(directory, "diagnostics.json");
                try (FileOutputStream stream = new FileOutputStream(temporary)) {
                    stream.write(text.getBytes(StandardCharsets.UTF_8));
                }
                if (!temporary.renameTo(destination)) error = "snapshot rename failed";
            }
        } catch (Exception failure) { display.setText("Diagnostic error: " + failure.getClass().getSimpleName()); }
    }

    @Override public void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        sensors.unregisterListener(this);
        manager.removeUpdates(listener);
        super.onDestroy();
    }
}
