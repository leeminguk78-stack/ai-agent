package io.github.leeminguk78.aiagent;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.WindowInsets;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;

import org.json.JSONObject;

import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Приложение «Агент» — оболочка пульта ИИ-агента.
 *  • показывает пульт http://127.0.0.1:8765 во весь экран;
 *  • если агент не запущен — запускает ~/agent-app/start.sh в Termux (RUN_COMMAND);
 *  • принимает «Поделиться → Агент» и вставляет текст в поле задачи;
 *  • превью сайтов и внешние ссылки открывает в браузере.
 */
public class MainActivity extends Activity {
    private static final String CONSOLE = "http://127.0.0.1:8765/";
    private static final String PING = CONSOLE + "api/config";
    private static final String START_PAGE = "file:///android_asset/start.html";
    private static final String TERMUX = "com.termux";
    private static final String PERM_RUN = "com.termux.permission.RUN_COMMAND";
    private static final String HOME = "/data/data/com.termux/files/home";
    private static final int REQ_RUN = 1;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private WebView web;
    private String pendingShare;
    private volatile boolean waiting;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#181b22"));
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0f1115"));
        root.addView(web, new FrameLayout.LayoutParams(-1, -1));
        setContentView(root);

        // Android 11+: рисуем от края до края и сами отступаем от статус-бара, выреза и клавиатуры
        if (Build.VERSION.SDK_INT >= 30) {
            getWindow().setDecorFitsSystemWindows(false);
            root.setOnApplyWindowInsetsListener((v, insets) -> {
                android.graphics.Insets s = insets.getInsets(WindowInsets.Type.systemBars()
                        | WindowInsets.Type.displayCutout() | WindowInsets.Type.ime());
                v.setPadding(s.left, s.top, s.right, s.bottom);
                return WindowInsets.CONSUMED;
            });
        }

        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        web.addJavascriptInterface(new Bridge(), "AgentApp");
        web.setWebViewClient(new Client());

        pendingShare = sharedText(getIntent());
        if (state == null || web.restoreState(state) == null) {
            open();
        }
    }

    /** Открыть пульт, если агент отвечает, иначе — экран запуска. */
    private void open() {
        new Thread(() -> {
            boolean up = ping();
            ui.post(() -> web.loadUrl(up ? CONSOLE : START_PAGE));
        }).start();
    }

    private static boolean ping() {
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(PING).openConnection();
            c.setConnectTimeout(1500);
            c.setReadTimeout(1500);
            int code = c.getResponseCode();
            c.disconnect();
            return code == 200;
        } catch (Exception e) {
            return false;
        }
    }

    /** Запуск агента: bash ~/agent-app/start.sh в фоне Termux. */
    private void startAgent() {
        if (checkSelfPermission(PERM_RUN) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{PERM_RUN}, REQ_RUN);
            return;
        }
        Intent i = new Intent();
        i.setClassName(TERMUX, "com.termux.app.RunCommandService");
        i.setAction("com.termux.RUN_COMMAND");
        i.putExtra("com.termux.RUN_COMMAND_PATH", "/data/data/com.termux/files/usr/bin/bash");
        i.putExtra("com.termux.RUN_COMMAND_ARGUMENTS", new String[]{HOME + "/agent-app/start.sh"});
        i.putExtra("com.termux.RUN_COMMAND_WORKDIR", HOME);
        i.putExtra("com.termux.RUN_COMMAND_BACKGROUND", true);
        try {
            if (startService(i) == null) {
                status("Termux не найден. Установи Termux и агента (см. README на GitHub).");
                return;
            }
        } catch (Exception e) {
            status("Termux отказал в запуске: " + e.getMessage());
            return;
        }
        status("Запускаю агента в Termux…");
        waitForAgent(30);
    }

    /** Ждём, пока агент начнёт отвечать, и открываем пульт. */
    private void waitForAgent(int seconds) {
        if (waiting) return;
        waiting = true;
        new Thread(() -> {
            for (int s = 0; s < seconds; s++) {
                if (ping()) {
                    waiting = false;
                    ui.post(() -> web.loadUrl(CONSOLE));
                    return;
                }
                try {
                    Thread.sleep(1000);
                } catch (InterruptedException ignored) {
                    break;
                }
            }
            waiting = false;
            ui.post(() -> status("Агент не ответил за " + seconds + " с. Проверь первую настройку Termux "
                    + "(allow-external-apps) или запусти вручную: bash ~/agent-app/start.sh"));
        }).start();
    }

    /** Показать сообщение на текущей странице (если она умеет setStatus). */
    private void status(String msg) {
        web.evaluateJavascript("window.setStatus&&setStatus(" + JSONObject.quote(msg) + ")", null);
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] res) {
        if (code != REQ_RUN) return;
        if (res.length > 0 && res[0] == PackageManager.PERMISSION_GRANTED) {
            startAgent();
        } else {
            status("Нужно разрешение «Запуск команд в среде Termux». "
                    + "Дай его в настройках приложения → Разрешения.");
        }
    }

    // ---------- «Поделиться → Агент» ----------
    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        String t = sharedText(intent);
        if (t != null) {
            pendingShare = t;
            injectShare();
        }
    }

    private static String sharedText(Intent i) {
        if (i == null || !Intent.ACTION_SEND.equals(i.getAction())) return null;
        CharSequence text = i.getCharSequenceExtra(Intent.EXTRA_TEXT);
        if (text == null) return null;
        String subject = i.getStringExtra(Intent.EXTRA_SUBJECT);
        return (subject != null && !subject.isEmpty() ? subject + "\n" : "") + text;
    }

    private void injectShare() {
        if (pendingShare == null) return;
        String js = "(function(){var e=document.getElementById('inp');if(!e)return false;"
                + "e.value=" + JSONObject.quote(pendingShare) + "+(e.value?'\\n'+e.value:'');"
                + "e.focus();return true})()";
        web.evaluateJavascript(js, r -> {
            if ("true".equals(r)) pendingShare = null;
        });
    }

    // ---------- мост для страниц: window.AgentApp ----------
    private class Bridge {
        @JavascriptInterface
        public void startAgent() {
            ui.post(MainActivity.this::startAgent);
        }

        @JavascriptInterface
        public void retry() {
            ui.post(MainActivity.this::open);
        }

        @JavascriptInterface
        public void openTermux() {
            ui.post(() -> {
                Intent t = getPackageManager().getLaunchIntentForPackage(TERMUX);
                if (t != null) startActivity(t);
                else status("Termux не установлен.");
            });
        }

        @JavascriptInterface
        public void openAppSettings() {
            ui.post(() -> {
                try {
                    startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                            Uri.parse("package:" + getPackageName())));
                } catch (ActivityNotFoundException ignored) {
                }
            });
        }
    }

    private class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
            Uri u = r.getUrl();
            if ("file".equals(u.getScheme())) return false;
            boolean local = "127.0.0.1".equals(u.getHost()) || "localhost".equals(u.getHost());
            if (local && u.getPort() == 8765) return false;      // пульт — внутри приложения
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, u)); // превью сайтов и ссылки — в браузере
            } catch (ActivityNotFoundException ignored) {
            }
            return true;
        }

        @Override
        public void onReceivedError(WebView v, WebResourceRequest r, WebResourceError e) {
            if (r.isForMainFrame() && r.getUrl().toString().startsWith(CONSOLE)) {
                v.loadUrl(START_PAGE);
            }
        }

        @Override
        public void onPageFinished(WebView v, String url) {
            if (url != null && url.startsWith(CONSOLE)) injectShare();
        }
    }

    // ---------- жизненный цикл ----------
    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
        // вернулись из Termux, где агента запустили вручную, — сразу открываем пульт
        if (START_PAGE.equals(web.getUrl())) open();
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
