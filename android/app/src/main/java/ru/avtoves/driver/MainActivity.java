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
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;

import com.google.firebase.messaging.FirebaseMessaging;

import org.json.JSONObject;

public class MainActivity extends Activity {
    private WebView webView;

    /** Мост для страницы приложения: она лежит внутри APK, чужие сайты сюда не загружаются. */
    private final class Bridge {
        @JavascriptInterface
        public void notify(String title, String text) {
            Notifier.show(MainActivity.this, title, text);
        }

        /** Открывает точку в установленном навигаторе (Яндекс Навигатор, Яндекс Карты и т. п.). */
        @JavascriptInterface
        public void openMap(double lat, double lon, String label) {
            if (Double.isNaN(lat) || Double.isNaN(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) {
                return;
            }
            String point = lat + "," + lon;
            Intent intent = new Intent(Intent.ACTION_VIEW,
                    Uri.parse("geo:" + point + "?q=" + point + "(" + Uri.encode(label == null ? "" : label) + ")"));
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            try {
                startActivity(intent);
            } catch (ActivityNotFoundException noNavigator) {
                startActivity(new Intent(Intent.ACTION_VIEW,
                        Uri.parse("https://yandex.ru/maps/?rtext=~" + point + "&rtt=auto")).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            }
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
        webView.loadUrl("file:///android_asset/app.html");
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
    public void onBackPressed() {
        webView.evaluateJavascript("window.avtovesBack && window.avtovesBack()", value -> {
            if (!"true".equals(value)) {
                finish();
            }
        });
    }
}
