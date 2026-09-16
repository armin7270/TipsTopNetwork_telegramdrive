package com.teledrive.app.ui

import android.app.AlertDialog
import android.app.DownloadManager
import android.app.ProgressDialog
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.provider.OpenableColumns
import android.view.View
import android.widget.*
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import com.google.android.material.floatingactionbutton.ExtendedFloatingActionButton
import com.teledrive.app.R
import com.teledrive.app.data.api.ApiClient
import com.teledrive.app.data.models.CreateFolderRequest
import com.teledrive.app.data.models.NodeDto
import com.teledrive.app.data.models.RenameNodeRequest
import com.teledrive.app.data.uploader.ChunkUploader
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.Locale

class MainActivity : AppCompatActivity() {

    private lateinit var tvBreadcrumb: TextView
    private lateinit var btnNewFolder: ImageButton
    private lateinit var btnLogout: ImageButton
    private lateinit var tvQuotaUsed: TextView
    private lateinit var tvServerStatus: TextView
    private lateinit var pbQuota: ProgressBar
    private lateinit var swipeRefresh: SwipeRefreshLayout
    private lateinit var rvFiles: RecyclerView
    private lateinit var llEmptyState: LinearLayout
    private lateinit var fabUpload: ExtendedFloatingActionButton

    private lateinit var fileAdapter: FileAdapter
    private val folderStack = mutableListOf<Pair<String?, String>>() // Pair(FolderId, FolderName)

    private val currentFolderId: String?
        get() = folderStack.lastOrNull()?.first

    private val filePickerLauncher = registerForActivityResult(ActivityResultContracts.GetContent()) { uri: Uri? ->
        uri?.let { startUpload(it) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Check authentication
        val token = ApiClient.getAccessToken(this)
        if (token.isNullOrBlank()) {
            startActivity(Intent(this, LoginActivity::class.java))
            finish()
            return
        }

        setContentView(R.layout.activity_main)

        initViews()
        setupRecyclerView()
        setupBackNavigation()

        // Initialize at root
        folderStack.add(Pair(null, "ریشه درایو"))
        loadCurrentFolder()
        loadQuotaUsage()
    }

    private fun initViews() {
        tvBreadcrumb = findViewById(R.id.tvBreadcrumb)
        btnNewFolder = findViewById(R.id.btnNewFolder)
        btnLogout = findViewById(R.id.btnLogout)
        tvQuotaUsed = findViewById(R.id.tvQuotaUsed)
        tvServerStatus = findViewById(R.id.tvServerStatus)
        pbQuota = findViewById(R.id.pbQuota)
        swipeRefresh = findViewById(R.id.swipeRefresh)
        rvFiles = findViewById(R.id.rvFiles)
        llEmptyState = findViewById(R.id.llEmptyState)
        fabUpload = findViewById(R.id.fabUpload)

        swipeRefresh.setOnRefreshListener {
            loadCurrentFolder()
            loadQuotaUsage()
        }

        btnNewFolder.setOnClickListener { showCreateFolderDialog() }
        btnLogout.setOnClickListener { showLogoutDialog() }
        fabUpload.setOnClickListener { filePickerLauncher.launch("*/*") }
    }

    private fun setupRecyclerView() {
        fileAdapter = FileAdapter(
            onItemClick = { node -> handleNodeClick(node) },
            onMenuClick = { node, anchorView -> showNodeMenu(node, anchorView) }
        )
        rvFiles.layoutManager = LinearLayoutManager(this)
        rvFiles.adapter = fileAdapter
    }

    private fun setupBackNavigation() {
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (folderStack.size > 1) {
                    folderStack.removeAt(folderStack.size - 1)
                    loadCurrentFolder()
                } else {
                    finish()
                }
            }
        })
    }

    private fun updateBreadcrumb() {
        val path = folderStack.joinToString(" / ") { it.second }
        tvBreadcrumb.text = "📁 $path"
    }

    private fun loadCurrentFolder() {
        swipeRefresh.isRefreshing = true
        updateBreadcrumb()

        lifecycleScope.launch {
            try {
                val api = ApiClient.getApi(this@MainActivity)
                val response = withContext(Dispatchers.IO) {
                    api.listChildren(parentId = currentFolderId)
                }

                if (response.isSuccessful && response.body() != null) {
                    val nodes = response.body()!!
                    fileAdapter.submitList(nodes)
                    llEmptyState.visibility = if (nodes.isEmpty()) View.VISIBLE else View.GONE
                } else {
                    Toast.makeText(this@MainActivity, "خطا در دریافت لیست فایل‌ها", Toast.LENGTH_SHORT).show()
                }
            } catch (e: Exception) {
                Toast.makeText(this@MainActivity, "خطای ارتباط با سرور: ${e.localizedMessage}", Toast.LENGTH_SHORT).show()
            } finally {
                swipeRefresh.isRefreshing = false
            }
        }
    }

    private fun loadQuotaUsage() {
        lifecycleScope.launch {
            try {
                val api = ApiClient.getApi(this@MainActivity)
                val response = withContext(Dispatchers.IO) { api.getUsage() }
                if (response.isSuccessful && response.body() != null) {
                    val usage = response.body()!!
                    val usedStr = formatBytes(usage.usedBytes)
                    val limitStr = if (usage.limitBytes > 0) formatBytes(usage.limitBytes) else "نامحدود"
                    tvQuotaUsed.text = "مصرف: $usedStr / $limitStr"

                    val pct = if (usage.limitBytes > 0) {
                        ((usage.usedBytes.toDouble() / usage.limitBytes.toDouble()) * 100).toInt().coerceIn(1, 100)
                    } else 5
                    pbQuota.progress = pct
                }
            } catch (_: Exception) {}
        }
    }

    private fun handleNodeClick(node: NodeDto) {
        if (node.kind == "folder") {
            folderStack.add(Pair(node.id, node.name))
            loadCurrentFolder()
        } else {
            val isMedia = isPlayableMedia(node.name, node.mimeType)
            if (isMedia) {
                val intent = Intent(this, PlayerActivity::class.java).apply {
                    putExtra(PlayerActivity.EXTRA_FILE_ID, node.id)
                    putExtra(PlayerActivity.EXTRA_FILE_NAME, node.name)
                }
                startActivity(intent)
            } else {
                showDownloadConfirmDialog(node)
            }
        }
    }

    private fun showNodeMenu(node: NodeDto, anchor: View) {
        val popup = PopupMenu(this, anchor)
        val isMedia = isPlayableMedia(node.name, node.mimeType)

        if (node.kind != "folder" && isMedia) {
            popup.menu.add(0, 1, 0, "▶️ پخش آنلاین")
        }
        if (node.kind != "folder") {
            popup.menu.add(0, 2, 1, "⬇️ دانلود مستقیم")
        }
        popup.menu.add(0, 3, 2, "✏️ تغییر نام")
        popup.menu.add(0, 4, 3, "🗑️ حذف")

        popup.setOnMenuItemClickListener { item ->
            when (item.itemId) {
                1 -> {
                    val intent = Intent(this, PlayerActivity::class.java).apply {
                        putExtra(PlayerActivity.EXTRA_FILE_ID, node.id)
                        putExtra(PlayerActivity.EXTRA_FILE_NAME, node.name)
                    }
                    startActivity(intent)
                    true
                }
                2 -> {
                    startDownload(node)
                    true
                }
                3 -> {
                    showRenameDialog(node)
                    true
                }
                4 -> {
                    showDeleteConfirmDialog(node)
                    true
                }
                else -> false
            }
        }
        popup.show()
    }

    private fun showCreateFolderDialog() {
        val input = EditText(this).apply {
            hint = "نام پوشه جدید"
            setPadding(40, 30, 40, 30)
        }

        AlertDialog.Builder(this)
            .setTitle("📁 ساخت پوشه جدید")
            .setView(input)
            .setPositiveButton("ایجاد") { _, _ ->
                val name = input.text.toString().trim()
                if (name.isNotBlank()) {
                    createFolder(name)
                }
            }
            .setNegativeButton("انصراف", null)
            .show()
    }

    private fun createFolder(name: String) {
        lifecycleScope.launch {
            try {
                val api = ApiClient.getApi(this@MainActivity)
                val resp = withContext(Dispatchers.IO) {
                    api.createFolder(CreateFolderRequest(parentId = currentFolderId, name = name))
                }
                if (resp.isSuccessful) {
                    Toast.makeText(this@MainActivity, "پوشه «$name» ساخته شد", Toast.LENGTH_SHORT).show()
                    loadCurrentFolder()
                } else {
                    Toast.makeText(this@MainActivity, "خطا در ساخت پوشه", Toast.LENGTH_SHORT).show()
                }
            } catch (e: Exception) {
                Toast.makeText(this@MainActivity, "خطا: ${e.localizedMessage}", Toast.LENGTH_SHORT).show()
            }
        }
    }

    private fun showRenameDialog(node: NodeDto) {
        val input = EditText(this).apply {
            setText(node.name)
            setPadding(40, 30, 40, 30)
        }

        AlertDialog.Builder(this)
            .setTitle("✏️ تغییر نام")
            .setView(input)
            .setPositiveButton("ذخیره") { _, _ ->
                val newName = input.text.toString().trim()
                if (newName.isNotBlank() && newName != node.name) {
                    renameNode(node.id, newName)
                }
            }
            .setNegativeButton("انصراف", null)
            .show()
    }

    private fun renameNode(nodeId: String, newName: String) {
        lifecycleScope.launch {
            try {
                val api = ApiClient.getApi(this@MainActivity)
                val resp = withContext(Dispatchers.IO) {
                    api.renameNode(nodeId, RenameNodeRequest(name = newName))
                }
                if (resp.isSuccessful) {
                    Toast.makeText(this@MainActivity, "نام تغییر یافت", Toast.LENGTH_SHORT).show()
                    loadCurrentFolder()
                } else {
                    Toast.makeText(this@MainActivity, "خطا در تغییر نام", Toast.LENGTH_SHORT).show()
                }
            } catch (e: Exception) {
                Toast.makeText(this@MainActivity, "خطا: ${e.localizedMessage}", Toast.LENGTH_SHORT).show()
            }
        }
    }

    private fun showDeleteConfirmDialog(node: NodeDto) {
        AlertDialog.Builder(this)
            .setTitle("حذف آیتم")
            .setMessage("آیا از حذف «${node.name}» مطمئن هستید؟")
            .setPositiveButton("بله، حذف کن") { _, _ ->
                deleteNode(node.id)
            }
            .setNegativeButton("انصراف", null)
            .show()
    }

    private fun deleteNode(nodeId: String) {
        lifecycleScope.launch {
            try {
                val api = ApiClient.getApi(this@MainActivity)
                val resp = withContext(Dispatchers.IO) { api.deleteNode(nodeId) }
                if (resp.isSuccessful) {
                    Toast.makeText(this@MainActivity, "با موفقیت حذف شد", Toast.LENGTH_SHORT).show()
                    loadCurrentFolder()
                    loadQuotaUsage()
                } else {
                    Toast.makeText(this@MainActivity, "خطا در حذف آیتم", Toast.LENGTH_SHORT).show()
                }
            } catch (e: Exception) {
                Toast.makeText(this@MainActivity, "خطا: ${e.localizedMessage}", Toast.LENGTH_SHORT).show()
            }
        }
    }

    private fun showDownloadConfirmDialog(node: NodeDto) {
        AlertDialog.Builder(this)
            .setTitle("دانلود فایل")
            .setMessage("آیا می‌خواهید فایل «${node.name}» (${formatBytes(node.sizeBytes)}) را دریافت کنید؟")
            .setPositiveButton("دانلود") { _, _ ->
                startDownload(node)
            }
            .setNegativeButton("انصراف", null)
            .show()
    }

    private fun startDownload(node: NodeDto) {
        try {
            val serverUrl = ApiClient.getServerUrl(this)
            val downloadUrl = "${serverUrl}api/v1/files/${node.id}/content"
            val token = ApiClient.getAccessToken(this)

            val request = DownloadManager.Request(Uri.parse(downloadUrl)).apply {
                setTitle(node.name)
                setDescription("در حال بارگیری از فضای ابری TeleDrive...")
                setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, node.name)
                if (!token.isNullOrBlank()) {
                    addRequestHeader("Authorization", "Bearer $token")
                }
            }

            val dm = getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
            dm.enqueue(request)
            Toast.makeText(this, "دانلود آغاز شد. پوشه بارگیری‌ها را چک کنید", Toast.LENGTH_LONG).show()
        } catch (e: Exception) {
            Toast.makeText(this, "خطا در دانلود فایل: ${e.localizedMessage}", Toast.LENGTH_LONG).show()
        }
    }

    private fun startUpload(uri: Uri) {
        val (fileName, fileSize) = queryFileInfo(uri)
        val mimeType = contentResolver.getType(uri)

        @Suppress("DEPRECATION")
        val progressDialog = ProgressDialog(this).apply {
            setTitle("☁️ آپلود به درایو تلگرام")
            setMessage("در حال آماده‌سازی فایل...")
            setProgressStyle(ProgressDialog.STYLE_HORIZONTAL)
            max = 100
            setCancelable(false)
            show()
        }

        lifecycleScope.launch {
            val uploader = ChunkUploader(this@MainActivity)
            uploader.uploadFile(
                fileUri = uri,
                fileName = fileName,
                fileSize = fileSize,
                mimeType = mimeType,
                parentId = currentFolderId,
                listener = object : ChunkUploader.UploadProgressListener {
                    override fun onProgress(chunkIndex: Int, totalChunks: Int, progressPct: Int, message: String) {
                        runOnUiThread {
                            progressDialog.progress = progressPct
                            progressDialog.setMessage(message)
                        }
                    }

                    override fun onSuccess(fileId: String, fileName: String) {
                        runOnUiThread {
                            progressDialog.dismiss()
                            Toast.makeText(this@MainActivity, "✅ «$fileName» با موفقیت ذخیره شد!", Toast.LENGTH_LONG).show()
                            loadCurrentFolder()
                            loadQuotaUsage()
                        }
                    }

                    override fun onError(error: String) {
                        runOnUiThread {
                            progressDialog.dismiss()
                            Toast.makeText(this@MainActivity, "❌ خطا در آپلود: $error", Toast.LENGTH_LONG).show()
                        }
                    }
                }
            )
        }
    }

    private fun queryFileInfo(uri: Uri): Pair<String, Long> {
        var name = "upload_${System.currentTimeMillis()}"
        var size = 0L

        contentResolver.query(uri, null, null, null, null)?.use { cursor ->
            if (cursor.moveToFirst()) {
                val nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                val sizeIndex = cursor.getColumnIndex(OpenableColumns.SIZE)

                if (nameIndex != -1) name = cursor.getString(nameIndex) ?: name
                if (sizeIndex != -1) size = cursor.getLong(sizeIndex)
            }
        }
        return Pair(name, size)
    }

    private fun showLogoutDialog() {
        AlertDialog.Builder(this)
            .setTitle("خروج از حساب")
            .setMessage("آیا می‌خواهید از حساب کاربری خارج شوید؟")
            .setPositiveButton("خروج") { _, _ ->
                ApiClient.clearAuth(this)
                startActivity(Intent(this, LoginActivity::class.java))
                finish()
            }
            .setNegativeButton("انصراف", null)
            .show()
    }

    private fun isPlayableMedia(name: String, mime: String?): Boolean {
        val ext = name.substringAfterLast('.', "").lowercase()
        return ext in listOf("mp4", "mkv", "avi", "mov", "webm", "mp3", "wav", "flac", "ogg", "m4a") ||
                mime?.startsWith("video/") == true || mime?.startsWith("audio/") == true
    }

    private fun formatBytes(bytes: Long): String {
        if (bytes <= 0) return "0 B"
        val units = arrayOf("B", "KB", "MB", "GB", "TB")
        val digitGroups = (Math.log10(bytes.toDouble()) / Math.log10(1024.0)).toInt().coerceIn(0, 4)
        return String.format(Locale.US, "%.1f %s", bytes / Math.pow(1024.0, digitGroups.toDouble()), units[digitGroups])
    }
}
