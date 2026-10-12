package com.edurun.diagnostic;

import android.app.Activity;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.location.Location;
import android.location.GnssStatus;
import android.location.OnNmeaMessageListener;
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
import java.lang.reflect.Method;
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
    private boolean gnssRegistered;
    private JSONObject injectionApi;
    private JSONObject injectionProbe = new JSONObject();
    private JSONArray injectedSamples = new JSONArray(), receivedProbeSamples = new JSONArray();
    private Method injectionInit, injectionSend;
    private boolean injecting;
    private int injectionTick;
    private long gnssStatusCount;
    private int satellitesTracked, satellitesUsedInFix;
    private float maxCn0DbHz;
    private boolean nmeaRegistered;
    private long nmeaCount;
    private String ggaTime = "", ggaSatellites = "", rmcTime = "", rmcDate = "", rmcValidity = "";
    private final OnNmeaMessageListener nmeaListener = new OnNmeaMessageListener() {
        @Override public void onNmeaMessage(String message, long timestamp) {
            nmeaCount++;
            String[] fields = message.trim().split(",", -1);
            if (fields.length > 9 && fields[0].equals("$GPRMC")) {
                rmcTime = fields[1]; rmcValidity = fields[2]; rmcDate = fields[9];
            } else if (fields.length > 7 && fields[0].equals("$GPGGA")) {
                ggaTime = fields[1]; ggaSatellites = fields[7];
            }
        }
    };
    private final GnssStatus.Callback gnssCallback = new GnssStatus.Callback() {
        @Override public void onSatelliteStatusChanged(GnssStatus status) {
            gnssStatusCount++;
            satellitesTracked = status.getSatelliteCount();
            satellitesUsedInFix = 0;
            maxCn0DbHz = 0;
            for (int i = 0; i < satellitesTracked; i++) {
                if (status.usedInFix(i)) satellitesUsedInFix++;
                maxCn0DbHz = Math.max(maxCn0DbHz, status.getCn0DbHz(i));
            }
        }
    };
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
        injectionApi = inspectInjectionApi();
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
        try { gnssRegistered = manager.registerGnssStatusCallback(gnssCallback, handler); }
        catch (SecurityException failure) { error = "gnss permission not granted"; }
        try { nmeaRegistered = manager.addNmeaListener(nmeaListener, handler); }
        catch (SecurityException failure) { error = "nmea permission not granted"; }
        handler.post(snapshot);
        // 必须由本项目诊断操作显式请求；默认普通监测不注入任何数据。
        if (getIntent().getBooleanExtra("probe_injection", false)) {
            handler.postDelayed(() -> beginInjectionProbe(), 300);
        }
    }

    private void beginInjectionProbe() {
        try {
            injectionProbe.put("synthetic_diagnostic_only", true);
            injectionProbe.put("state", "initializing");
            injectionInit = SensorManager.class.getMethod("initDataInjection", boolean.class);
            injectionSend = SensorManager.class.getMethod("injectSensorData",
                Sensor.class, float[].class, int.class, long.class);
            injecting = Boolean.TRUE.equals(injectionInit.invoke(sensors, true));
            injectionProbe.put("initialized", injecting);
            if (!injecting) { injectionProbe.put("state", "not_supported"); return; }
            handler.post(injectionSample);
        } catch (Exception failure) {
            finishInjectionProbe("api_rejected");
        }
    }

    /** 一秒、25Hz、有界小幅测试信号；不是自然步伐，更不会生成18/19计步传感器。 */
    private final Runnable injectionSample = new Runnable() {
        @Override public void run() {
            if (!injecting) return;
            try {
                int[] types = {Sensor.TYPE_ACCELEROMETER, Sensor.TYPE_GYROSCOPE};
                float variation = (float) (0.5 * Math.sin(injectionTick * Math.PI / 2));
                for (int type : types) {
                    Sensor sensor = sensors.getDefaultSensor(type);
                    if (sensor == null) { finishInjectionProbe("sensor_missing"); return; }
                    float[] values = type == Sensor.TYPE_ACCELEROMETER
                        ? new float[]{0.1f, 9.8f + variation, 0.2f}
                        : new float[]{0, 0, variation * 0.04f};
                    long timestamp = SystemClock.elapsedRealtimeNanos();
                    boolean accepted = Boolean.TRUE.equals(injectionSend.invoke(sensors,
                        sensor, values, SensorManager.SENSOR_STATUS_ACCURACY_HIGH, timestamp));
                    if (!accepted) { finishInjectionProbe("send_rejected"); return; }
                    injectedSamples.put(probeSample(type, timestamp, values));
                }
                if (++injectionTick >= 25) {
                    // 留出接收回调时间；单独关闭客户端并不证明HAL已恢复NORMAL。
                    handler.postDelayed(() -> finishInjectionProbe("completed"), 200);
                } else handler.postDelayed(this, 40);
            } catch (Exception failure) { finishInjectionProbe("api_rejected"); }
        }
    };

    private static JSONObject probeSample(int type, long timestamp, float[] values) throws Exception {
        JSONObject result = new JSONObject();
        result.put("type", type); result.put("timestamp_ns", timestamp);
        JSONArray vector = new JSONArray(); for (float value : values) vector.put(value);
        result.put("values", vector); return result;
    }

    private void finishInjectionProbe(String state) {
        handler.removeCallbacks(injectionSample);
        try {
            injectionProbe.put("state", state);
            if (injecting && injectionInit != null) {
                injectionProbe.put("client_closed", Boolean.TRUE.equals(injectionInit.invoke(sensors, false)));
            }
        } catch (Exception failure) {
            try { injectionProbe.put("client_closed", false); } catch (Exception ignored) {}
        } finally { injecting = false; }
    }

    /** 仅探测当前系统允许访问的方法；不调用、不放宽hidden API或更改传感器模式。 */
    private JSONObject inspectInjectionApi() {
        JSONObject result = new JSONObject();
        try {
            result.put("probe_only", true);
            result.put("mode_changed", false);
            String[] names = {"initDataInjection", "injectSensorData"};
            Class<?>[][] parameters = {{boolean.class},
                {Sensor.class, float[].class, int.class, long.class}};
            for (int i = 0; i < names.length; i++) {
                try {
                    SensorManager.class.getMethod(names[i], parameters[i]);
                    result.put(names[i], "available");
                } catch (NoSuchMethodException failure) {
                    result.put(names[i], "not_accessible");
                } catch (SecurityException failure) {
                    result.put(names[i], "permission_denied");
                }
            }
        } catch (Exception failure) {
            // 不保存异常参数或栈；失败不冒充支持。
        }
        return result;
    }

    @Override public void onSensorChanged(SensorEvent event) {
        if (injecting && receivedProbeSamples.length() < 1024
            && (event.sensor.getType() == Sensor.TYPE_ACCELEROMETER
                || event.sensor.getType() == Sensor.TYPE_GYROSCOPE)) {
            try { receivedProbeSamples.put(probeSample(event.sensor.getType(), event.timestamp, event.values)); }
            catch (Exception ignored) {}
        }
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
            output.put("injection_api", injectionApi);
            injectionProbe.put("sent_samples", injectedSamples);
            injectionProbe.put("received_samples", receivedProbeSamples);
            output.put("injection_probe", injectionProbe);
            JSONObject gnss = new JSONObject();
            gnss.put("registered", gnssRegistered);
            gnss.put("status_callback_count", gnssStatusCount);
            gnss.put("satellites_tracked", satellitesTracked);
            gnss.put("satellites_used_in_fix", satellitesUsedInFix);
            gnss.put("max_cn0_dbhz", maxCn0DbHz);
            output.put("gnss", gnss);
            JSONObject nmea = new JSONObject();
            nmea.put("registered", nmeaRegistered);
            nmea.put("message_count", nmeaCount);
            nmea.put("gga_time", ggaTime);
            nmea.put("gga_satellites", ggaSatellites);
            nmea.put("rmc_time", rmcTime);
            nmea.put("rmc_date", rmcDate);
            nmea.put("rmc_validity", rmcValidity);
            output.put("nmea", nmea);
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
        if (injecting) finishInjectionProbe("activity_destroyed");
        handler.removeCallbacksAndMessages(null);
        sensors.unregisterListener(this);
        manager.removeUpdates(listener);
        if (gnssRegistered) manager.unregisterGnssStatusCallback(gnssCallback);
        if (nmeaRegistered) manager.removeNmeaListener(nmeaListener);
        super.onDestroy();
    }
}
