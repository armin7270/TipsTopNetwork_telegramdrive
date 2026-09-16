package com.teledrive.app.ui

import android.content.Intent
import android.os.Bundle
import android.view.View
import android.widget.Button
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.google.android.material.textfield.TextInputEditText
import com.google.android.material.textfield.TextInputLayout
import com.teledrive.app.R
import com.teledrive.app.data.api.ApiClient
import com.teledrive.app.data.models.LoginRequest
import com.teledrive.app.data.models.RegisterRequest
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class LoginActivity : AppCompatActivity() {

    private lateinit var tvFormTitle: TextView
    private lateinit var tilDisplayName: TextInputLayout
    private lateinit var etDisplayName: TextInputEditText
    private lateinit var etEmail: TextInputEditText
    private lateinit var etPassword: TextInputEditText
    private lateinit var btnSubmit: Button
    private lateinit var tvToggleMode: TextView
    private lateinit var btnServerConfig: Button

    private var isRegisterMode = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_login)

        tvFormTitle = findViewById(R.id.tvFormTitle)
        tilDisplayName = findViewById(R.id.tilDisplayName)
        etDisplayName = findViewById(R.id.etDisplayName)
        etEmail = findViewById(R.id.etEmail)
        etPassword = findViewById(R.id.etPassword)
        btnSubmit = findViewById(R.id.btnSubmit)
        tvToggleMode = findViewById(R.id.tvToggleMode)
        btnServerConfig = findViewById(R.id.btnServerConfig)

        btnServerConfig.setOnClickListener {
            startActivity(Intent(this, ServerConfigActivity::class.java))
        }

        tvToggleMode.setOnClickListener {
            isRegisterMode = !isRegisterMode
            updateUiMode()
        }

        btnSubmit.setOnClickListener {
            val email = etEmail.text.toString().trim()
            val password = etPassword.text.toString().trim()

            if (email.isBlank() || password.isBlank()) {
                Toast.makeText(this, "لطفاً ایمیل و رمز عبور را وارد کنید", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }

            if (isRegisterMode) {
                val displayName = etDisplayName.text.toString().trim()
                if (displayName.isBlank()) {
                    Toast.makeText(this, "لطفاً نام نمایشی را وارد کنید", Toast.LENGTH_SHORT).show()
                    return@setOnClickListener
                }
                performRegister(email, password, displayName)
            } else {
                performLogin(email, password)
            }
        }
    }

    private fun updateUiMode() {
        if (isRegisterMode) {
            tvFormTitle.text = getString(R.string.register_title)
            tilDisplayName.visibility = View.VISIBLE
            btnSubmit.text = getString(R.string.btn_register)
            tvToggleMode.text = "حساب کاربری دارید؟ وارد شوید"
        } else {
            tvFormTitle.text = getString(R.string.login_title)
            tilDisplayName.visibility = View.GONE
            btnSubmit.text = getString(R.string.btn_login)
            tvToggleMode.text = "حساب کاربری ندارید؟ ثبت‌نام کنید"
        }
    }

    private fun performLogin(email: String, pass: String) {
        btnSubmit.isEnabled = false
        btnSubmit.text = "در حال ورود..."

        lifecycleScope.launch {
            try {
                val api = ApiClient.getApi(this@LoginActivity)
                val response = withContext(Dispatchers.IO) {
                    api.login(LoginRequest(email = email, password = pass))
                }

                if (response.isSuccessful && response.body() != null) {
                    val auth = response.body()!!
                    ApiClient.saveTokens(this@LoginActivity, auth.accessToken, auth.refreshToken)
                    Toast.makeText(this@LoginActivity, "خوش آمدید!", Toast.LENGTH_SHORT).show()

                    val intent = Intent(this@LoginActivity, MainActivity::class.java)
                    startActivity(intent)
                    finish()
                } else {
                    val err = response.errorBody()?.string() ?: "خطای ناشناخته"
                    Toast.makeText(this@LoginActivity, "خطا در ورود: $err", Toast.LENGTH_LONG).show()
                }
            } catch (e: Exception) {
                Toast.makeText(this@LoginActivity, "خطای ارتباط با سرور: ${e.localizedMessage}", Toast.LENGTH_LONG).show()
            } finally {
                btnSubmit.isEnabled = true
                btnSubmit.text = getString(R.string.btn_login)
            }
        }
    }

    private fun performRegister(email: String, pass: String, displayName: String) {
        btnSubmit.isEnabled = false
        btnSubmit.text = "در حال ثبت‌نام..."

        lifecycleScope.launch {
            try {
                val api = ApiClient.getApi(this@LoginActivity)
                val response = withContext(Dispatchers.IO) {
                    api.register(RegisterRequest(email = email, password = pass, displayName = displayName))
                }

                if (response.isSuccessful && response.body() != null) {
                    val auth = response.body()!!
                    ApiClient.saveTokens(this@LoginActivity, auth.accessToken, auth.refreshToken)
                    Toast.makeText(this@LoginActivity, "ثبت‌نام با موفقیت انجام شد!", Toast.LENGTH_SHORT).show()

                    val intent = Intent(this@LoginActivity, MainActivity::class.java)
                    startActivity(intent)
                    finish()
                } else {
                    val err = response.errorBody()?.string() ?: "خطای ثبت‌نام"
                    Toast.makeText(this@LoginActivity, "خطا: $err", Toast.LENGTH_LONG).show()
                }
            } catch (e: Exception) {
                Toast.makeText(this@LoginActivity, "خطای ارتباط با سرور: ${e.localizedMessage}", Toast.LENGTH_LONG).show()
            } finally {
                btnSubmit.isEnabled = true
                btnSubmit.text = getString(R.string.btn_register)
            }
        }
    }
}
