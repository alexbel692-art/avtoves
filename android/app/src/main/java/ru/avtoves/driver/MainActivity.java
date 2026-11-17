package ru.avtoves.driver;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.graphics.Color;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.window.OnBackInvokedCallback;
import android.window.OnBackInvokedDispatcher;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;

import com.google.firebase.messaging.FirebaseMessaging;

import org.json.JSONObject;

public class MainActivity extends Activity {
    private WebView webView;
    private static final String POINT = "-?\\d+(\\.\\d+)?,-?\\d+(\\.\\d+)?";
    private static final String POINTS = POINT + "(~" + POINT + "){0,49}";

    /** Мост для страницы приложения: она лежит внутри APK, чужие сайты сюда не загружаются. */
    private final class Bridge {
        @JavascriptInterface
        public void notify(String title, String text) {
            Notifier.show(MainActivity.this, title, text);
        }

        /** Одна точка: маршрут от текущего места до клиента в Яндекс Картах. */
        @JavascriptInterface
        public void openMap(double lat, double lon, String label) {
            if (!valid(lat, lon)) {
                return;
            }
            openRoute(lat + "," + lon);
        }

        /** Несколько точек «шир,долг~шир,долг~…» в порядке объезда: маршрут от текущего места через все. */
        @JavascriptInterface
        public void openRoute(String points) {
            if (points == null || !points.matches("-?\\d+(\\.\\d+)?,-?\\d+(\\.\\d+)?(~-?\\d+(\\.\\d+)?,-?\\d+(\\.\\d+)?){0,49}")) {
                return;
            }
            String query = "?rtext=~" + points + "&rtt=auto";
            // Сначала приложение Яндекс Карты, если его нет — Яндекс Карты в браузере
            Intent app = new Intent(Intent.ACTION_VIEW, Uri.parse("yandexmaps://maps.yandex.ru/" + query))
                    .setPackage("ru.yandex.yandexmaps").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            try {
                startActivity(app);
            } catch (ActivityNotFoundException noApp) {
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse("https://yandex.ru/maps/" + query))
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            }
        }

        /** Звонок клиенту: открывается набор номера (разрешение на звонки не нужно — водитель жмёт «Вызов»). */
        @JavascriptInterface
        public void dial(String phone) {
            if (phone == null || !phone.matches("\\+?\\d{5,15}")) {
                return;
            }
            start(new Intent(Intent.ACTION_DIAL, Uri.parse("tel:" + phone)));
        }

        /**
         * Маршрут от текущего места водителя: app = "navi" (Яндекс Навигатор) или "maps" (Яндекс Карты).
         * points — «шир,долг~…» в порядке объезда; если координат нет — text, адрес для поиска.
         * Нет Навигатора — открываются Карты, нет Карт — Яндекс Карты в браузере.
         */
        @JavascriptInterface
        public void route(String app, String points, String text) {
            boolean hasPoints = points != null && points.matches(POINTS);
            String address = text == null ? "" : text.trim();
            if (!hasPoints && (address.isEmpty() || address.length() > 300)) {
                return;
            }
            String[] list = hasPoints ? points.split("~") : new String[0];
            if ("navi".equals(app)) {
                String navi;
                if (hasPoints) {
                    String[] to = list[list.length - 1].split(",");
                    StringBuilder q = new StringBuilder("yandexnavi://build_route_on_map?lat_to=" + to[0] + "&lon_to=" + to[1]);
                    for (int i = 0; i < list.length - 1; i++) {        // промежуточные точки по порядку объезда
                        String[] via = list[i].split(",");
                        q.append("&lat_via_").append(i).append('=').append(via[0])
                                .append("&lon_via_").append(i).append('=').append(via[1]);
                    }
                    navi = q.toString();
                } else {
                    navi = "yandexnavi://map_search?text=" + Uri.encode(address);
                }
                if (start(new Intent(Intent.ACTION_VIEW, Uri.parse(navi)).setPackage("ru.yandex.yandexnavi"))) {
                    return;
                }
            }
            String query = hasPoints ? "?rtext=~" + points + "&rtt=auto" : "?rtext=~" + Uri.encode(address) + "&rtt=auto";
            if (start(new Intent(Intent.ACTION_VIEW, Uri.parse("yandexmaps://maps.yandex.ru/" + query))
                    .setPackage("ru.yandex.yandexmaps"))) {
                return;
            }
            start(new Intent(Intent.ACTION_VIEW, Uri.parse("https://yandex.ru/maps/" + query)));
        }

        private boolean start(Intent intent) {
            try {
                startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
                return true;
            } catch (ActivityNotFoundException noApp) {
                return false;
            }
        }

        private boolean valid(double lat, double lon) {
            return !Double.isNaN(lat) && !Double.isNaN(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180;
        }

        @JavascriptInterface
        public void requestPush() {
            runOnUiThread(MainActivity.this::setUpPush);
        }
    }

    @SuppressLint({"SetJavaScriptEnabled", "JavascriptInterface"})
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Notifier.ensureChannel(this);
        webView = new WebView(this);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        webView.addJavascriptInterface(new Bridge(), "Android");
        setContentView(webView);
        fitSystemBars();
        if (Build.VERSION.SDK_INT >= 33) {
            // Android 16 больше не вызывает onBackPressed: «Назад» приходит сюда
            getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                    OnBackInvokedDispatcher.PRIORITY_DEFAULT, (OnBackInvokedCallback) this::goBack);
        }
        webView.loadUrl("file:///android_asset/app.html");
    }

    /** С Android 15 окно занимает весь экран: отодвигаем страницу от строки состояния, жестов и клавиатуры. */
    private void fitSystemBars() {
        int steel = Color.rgb(0xDF, 0xE5, 0xEA);              // фон страницы приложения
        getWindow().getDecorView().setBackgroundColor(steel);
        webView.setBackgroundColor(steel);
        if (Build.VERSION.SDK_INT >= 30) {
            WindowInsetsController bars = getWindow().getInsetsController();
            if (bars != null) {                                // тёмные значки на светлом фоне
                int light = WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS
                        | WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS;
                bars.setSystemBarsAppearance(light, light);
            }
            webView.setOnApplyWindowInsetsListener((View v, WindowInsets insets) -> {
                android.graphics.Insets i = insets.getInsets(WindowInsets.Type.systemBars()
                        | WindowInsets.Type.displayCutout() | WindowInsets.Type.ime());
                v.setPadding(i.left, i.top, i.right, i.bottom);
                return WindowInsets.CONSUMED;
            });
        }
    }

    /** «Назад»: сначала закрывается открытый экран приложения, на главном — выход. */
    private void goBack() {
        webView.evaluateJavascript("window.avtovesBack && window.avtovesBack()", value -> {
            if (!"true".equals(value)) {
                finish();
            }
        });
    }

    private void setUpPush() {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        try {
            FirebaseMessaging.getInstance().getToken().addOnCompleteListener(task -> {
                if (task.isSuccessful() && task.getResult() != null) {
                    webView.evaluateJavascript("window.avtovesPushToken && window.avtovesPushToken("
                            + JSONObject.quote(task.getResult()) + ")", null);
                }
            });
        } catch (RuntimeException ignored) {
            // приложение собрано без google-services.json: уведомления через Google выключены
        }
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {           // Android 12 и старше
        goBack();
    }
}
