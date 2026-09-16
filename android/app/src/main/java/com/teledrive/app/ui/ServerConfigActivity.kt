package com.teledrive.app.ui

import android.os.Bundle
import android.widget.Button
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.google.android.material.textfield.TextInputEditText
import com.teledrive.app.R
import com.teledrive.app.data.api.ApiClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class ServerConfigActivity : AppCompatActivity() {

    private lateinit var etServerUrl: TextInputEditText
    private lateinit var btnTestPing: Button
    private lateinit var btnSaveServer: Button

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_server_config)

        etServerUrl = findViewById(R.id.etServerUrl)
        btnTestPing = findViewById(R.id.btnTestPing)
        btnSaveServer = findViewById(R.id.btnSaveServer)

        val currentUrl = ApiClient.getServerUrl(this)
        etServerUrl.setText(currentUrl)

        btnTestPing.setOnClickListener {
            val candidateUrl = etServerUrl.text.toString().trim()
            if (candidateUrl.isBlank()) {
                Toast.makeText(this, "لطفاً آدرس سرور را وارد کنید", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            testConnection(candidateUrl)
        }

        btnSaveServer.setOnClickListener {
            val candidateUrl = etServerUrl.text.toString().trim()
            if (candidateUrl.isBlank()) {
                Toast.makeText(this, "لطفاً آدرس سرور را وارد کنید", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            ApiClient.setServerUrl(this, candidateUrl)
            Toast.makeText(this, "آدرس سرور با موفقیت ذخیره شد", Toast.LENGTH_SHORT).show()
            finish()
        }
    }

    private fun testConnection(url: String) {
        btnTestPing.isEnabled = false
        btnTestPing.text = "در حال برقراری اتصال..."

        lifecycleScope.launch {
            try {
                // Temporarily test connection using custom Retrofit or direct ping
                val formattedUrl = if (url.endsWith("/")) url else "$url/"
                val okHttpClient = okhttp3.OkHttpClient.Builder()
                    .connectTimeout(5, java.util.concurrent.TimeUnit.SECONDS)
                    .readTimeout(5, java.util.concurrent.TimeUnit.SECONDS)
                    .build()

                val request = okhttp3.Request.Builder()
                    .url("${formattedUrl}readyz")
                    .get()
                    .build()

                val isSuccess = withContext(Dispatchers.IO) {
                    try {
                        val response = okHttpClient.newCall(request).execute()
                        response.isSuccessful
                    } catch (e: Exception) {
                        false
                    }
                }

                if (isSuccess) {
                    Toast.makeText(this@ServerConfigActivity, "✅ اتصال به سرور موفقیت‌آمیز بود!", Toast.LENGTH_LONG).show()
                } else {
                    Toast.makeText(this@ServerConfigActivity, "⚠️ سرور در دسترس است اما خطای وضعیت دریافت شد", Toast.LENGTH_LONG).show()
                }
            } catch (e: Exception) {
                Toast.makeText(this@ServerConfigActivity, "❌ خطا در برقراری ارتباط: ${e.localizedMessage}", Toast.LENGTH_LONG).show()
            } finally {
                btnTestPing.isEnabled = true
                btnTestPing.text = "🔍 تست اتصال به سرور"
            }
        }
    }
}
