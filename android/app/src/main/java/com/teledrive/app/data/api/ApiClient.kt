package com.teledrive.app.data.api

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit

object ApiClient {

    private const val PREFS_NAME = "teledrive_secure_prefs"
    private const val KEY_SERVER_URL = "server_url"
    private const val KEY_ACCESS_TOKEN = "access_token"
    private const val KEY_REFRESH_TOKEN = "refresh_token"

    // Default fallback server URL
    const val DEFAULT_SERVER_URL = "http://10.0.2.2:8000/"

    private var retrofit: Retrofit? = null
    private var apiService: TeleDriveApi? = null

    private fun getEncryptedPrefs(context: Context): SharedPreferences {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        return EncryptedSharedPreferences.create(
            context,
            PREFS_NAME,
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
        )
    }

    fun getServerUrl(context: Context): String {
        val url = getEncryptedPrefs(context).getString(KEY_SERVER_URL, DEFAULT_SERVER_URL) ?: DEFAULT_SERVER_URL
        return if (url.endsWith("/")) url else "$url/"
    }

    fun setServerUrl(context: Context, url: String) {
        val formatted = if (url.endsWith("/")) url else "$url/"
        getEncryptedPrefs(context).edit().putString(KEY_SERVER_URL, formatted).apply()
        retrofit = null
        apiService = null
    }

    fun getAccessToken(context: Context): String? {
        return getEncryptedPrefs(context).getString(KEY_ACCESS_TOKEN, null)
    }

    fun saveTokens(context: Context, accessToken: String, refreshToken: String) {
        getEncryptedPrefs(context).edit()
            .putString(KEY_ACCESS_TOKEN, accessToken)
            .putString(KEY_REFRESH_TOKEN, refreshToken)
            .apply()
    }

    fun clearAuth(context: Context) {
        getEncryptedPrefs(context).edit()
            .remove(KEY_ACCESS_TOKEN)
            .remove(KEY_REFRESH_TOKEN)
            .apply()
    }

    fun getApi(context: Context): TeleDriveApi {
        if (apiService != null) return apiService!!

        val baseUrl = getServerUrl(context)

        val authInterceptor = Interceptor { chain ->
            val original = chain.request()
            val token = getAccessToken(context)
            val builder = original.newBuilder()
            if (!token.isNullOrBlank()) {
                builder.header("Authorization", "Bearer $token")
            }
            chain.proceed(builder.build())
        }

        val logging = HttpLoggingInterceptor().apply {
            level = HttpLoggingInterceptor.Level.BASIC
        }

        val okHttpClient = OkHttpClient.Builder()
            .addInterceptor(authInterceptor)
            .addInterceptor(logging)
            .connectTimeout(60, TimeUnit.SECONDS)
            .readTimeout(120, TimeUnit.SECONDS)
            .writeTimeout(120, TimeUnit.SECONDS)
            .build()

        val newRetrofit = Retrofit.Builder()
            .baseUrl(baseUrl)
            .client(okHttpClient)
            .addConverterFactory(GsonConverterFactory.create())
            .build()

        retrofit = newRetrofit
        val service = newRetrofit.create(TeleDriveApi::class.java)
        apiService = service
        return service
    }
}
