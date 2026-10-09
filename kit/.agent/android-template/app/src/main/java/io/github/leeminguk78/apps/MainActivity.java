package io.github.leeminguk78.apps;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;

import java.io.ByteArrayInputStream;
import java.io.InputStream;
import java.util.HashMap;
import java.util.Map;

/**
 * Оболочка веб-приложения, созданного ИИ-агентом.
 * Файлы из assets/www открываются как https://app.local/… — это обычный «сайт» без интернета:
 * работают localStorage, ES-модули, fetch() к своим файлам, alert/confirm.
 * Внешние ссылки открываются в браузере.
 */
public class MainActivity extends Activity {
    private static final String HOST = "app.local";
    private static final String START = "https://" + HOST + "/index.html";
    private static final Map<String, String> MIME = new HashMap<>();

    static {
        String[][] m = {
            {"html", "text/html"}, {"htm", "text/html"}, {"js", "text/javascript"}, {"mjs", "text/javascript"},
            {"css", "text/css"}, {"json", "application/json"}, {"txt", "text/plain"}, {"svg", "image/svg+xml"},
            {"png", "image/png"}, {"jpg", "image/jpeg"}, {"jpeg", "image/jpeg"}, {"gif", "image/gif"},
            {"webp", "image/webp"}, {"ico", "image/x-icon"}, {"woff", "font/woff"}, {"woff2", "font/woff2"},
            {"ttf", "font/ttf"}, {"otf", "font/otf"}, {"mp3", "audio/mpeg"}, {"wav", "audio/wav"},
            {"ogg", "audio/ogg"}, {"mp4", "video/mp4"}, {"webm", "video/webm"}, {"wasm", "application/wasm"},
            {"webmanifest", "application/manifest+json"}
        };
        for (String[] p : m) MIME.put(p[0], p[1]);
    }

    private WebView web;

    @Override
    @SuppressWarnings("deprecation")
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        int bg = getColor(R.color.app_bg);
        boolean dark = getResources().getBoolean(R.bool.app_dark);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(bg);
        web = new WebView(this);
        web.setBackgroundColor(bg);
        root.addView(web, new FrameLayout.LayoutParams(-1, -1));
        setContentView(root);

        if (Build.VERSION.SDK_INT >= 30) {
            // от края до края: сами отступаем от статус-бара, выреза и клавиатуры
            getWindow().setDecorFitsSystemWindows(false);
            root.setOnApplyWindowInsetsListener((v, insets) -> {
                android.graphics.Insets s = insets.getInsets(WindowInsets.Type.systemBars()
                        | WindowInsets.Type.displayCutout() | WindowInsets.Type.ime());
                v.setPadding(s.left, s.top, s.right, s.bottom);
                return WindowInsets.CONSUMED;
            });
            WindowInsetsController c = getWindow().getInsetsController();
            if (c != null) {
                int light = WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS
                        | WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS;
                c.setSystemBarsAppearance(dark ? 0 : light, light);
            }
        } else if (!dark) {
            root.setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR);
        }

        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setDatabaseEnabled(true);
        ws.setMediaPlaybackRequiresUserGesture(false);
        web.setWebChromeClient(new WebChromeClient());   // alert/confirm/prompt
        web.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView v, WebResourceRequest r) {
                Uri u = r.getUrl();
                return HOST.equals(u.getHost()) ? asset(u.getPath()) : null;
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                Uri u = r.getUrl();
                if (HOST.equals(u.getHost())) return false;
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, u));
                } catch (ActivityNotFoundException ignored) {
                }
                return true;
            }
        });

        if (state == null || web.restoreState(state) == null) web.loadUrl(START);
    }

    /** Файл из assets/www по пути из адреса https://app.local/… */
    private WebResourceResponse asset(String path) {
        if (path == null || path.isEmpty() || path.equals("/")) path = "/index.html";
        if (path.endsWith("/")) path += "index.html";
        if (path.contains("..")) return notFound();
        String file = "www" + path;
        String ext = file.substring(file.lastIndexOf('.') + 1).toLowerCase();
        String mime = MIME.containsKey(ext) ? MIME.get(ext) : "application/octet-stream";
        boolean text = mime.startsWith("text/") || mime.endsWith("json") || mime.endsWith("javascript");
        try {
            InputStream in = getAssets().open(file);
            WebResourceResponse res = new WebResourceResponse(mime, text ? "utf-8" : null, in);
            Map<String, String> h = new HashMap<>();
            h.put("Cache-Control", "no-cache");
            h.put("Access-Control-Allow-Origin", "*");
            res.setResponseHeaders(h);
            return res;
        } catch (Exception e) {
            return notFound();
        }
    }

    private static WebResourceResponse notFound() {
        return new WebResourceResponse("text/plain", "utf-8", 404, "Not Found", new HashMap<>(),
                new ByteArrayInputStream("Not found".getBytes()));
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
    }

    @Override
    protected void onPause() {
        web.onPause();
        super.onPause();
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        web.destroy();
        super.onDestroy();
    }
}
