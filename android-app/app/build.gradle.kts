plugins {
    id("com.android.application")
}

// Номер сборки GitHub Actions = номер версии: каждое обновление ставится поверх предыдущего.
val build = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()

android {
    namespace = "io.github.leeminguk78.aiagent"
    compileSdk = 35

    defaultConfig {
        applicationId = "io.github.leeminguk78.aiagent"
        minSdk = 26
        targetSdk = 35
        versionCode = build
        versionName = "1.0.$build"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
