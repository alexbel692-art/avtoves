package ru.avtoves.driver;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.os.Bundle;
import android.webkit.WebSettings;
import android.webkit.WebView;

public class MainActivity extends Activity {
    private WebView webView;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        webView = new WebView(this);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        setContentView(webView);
        webView.loadUrl("file:///android_asset/app.html");
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
