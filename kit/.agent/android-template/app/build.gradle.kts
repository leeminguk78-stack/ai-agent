plugins {
    id("com.android.application")
}

// Параметры приложения приходят из workflow (app.json): id, название, тема, цвет фона.
fun env(name: String, default: String) = System.getenv(name)?.takeIf { it.isNotBlank() } ?: default
fun androidString(s: String) = s.replace("\\", "\\\\").replace("&", "&amp;").replace("<", "&lt;")
    .replace(">", "&gt;").replace("'", "\\'").replace("\"", "\\\"")

val appId = env("APP_ID", "demo")
val appName = env("APP_NAME", "Демо")
val appColor = env("APP_COLOR", "#ffffff")
val appDark = env("APP_THEME", "light") == "dark"
val build = env("GITHUB_RUN_NUMBER", "1").toInt()

android {
    namespace = "io.github.leeminguk78.apps"
    compileSdk = 35

    defaultConfig {
        applicationId = "io.github.leeminguk78.apps.$appId"
        minSdk = 26
        targetSdk = 35
        versionCode = build
        versionName = "1.0.$build"
        resValue("string", "app_name", androidString(appName))
        resValue("color", "app_bg", appColor)
        resValue("bool", "app_dark", appDark.toString())
    }

    // Постоянный ключ подписи (путь передаёт workflow); локально — обычная отладочная подпись.
    val keystore = System.getenv("AGENT_KEYSTORE")
    signingConfigs {
        if (keystore != null) {
            create("apps") {
                storeFile = file(keystore)
                storePassword = "agent-apps"
                keyAlias = "apps"
                keyPassword = "agent-apps"
            }
        }
    }
    buildTypes {
        getByName("debug") {
            if (keystore != null) signingConfig = signingConfigs.getByName("apps")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
