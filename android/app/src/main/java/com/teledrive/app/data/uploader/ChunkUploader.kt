package com.teledrive.app.data.uploader

import android.content.Context
import android.net.Uri
import com.teledrive.app.data.api.ApiClient
import com.teledrive.app.data.models.CompleteUploadRequest
import com.teledrive.app.data.models.CreateUploadRequest
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.InputStream
import java.security.MessageDigest

class ChunkUploader(private val context: Context) {

    companion object {
        const val CHUNK_SIZE = 1 * 1024 * 1024 // 1 MiB standard TeleDrive chunk size
    }

    interface UploadProgressListener {
        fun onProgress(chunkIndex: Int, totalChunks: Int, progressPct: Int, message: String)
        fun onSuccess(fileId: String, fileName: String)
        fun onError(error: String)
    }

    suspend fun uploadFile(
        fileUri: Uri,
        fileName: String,
        fileSize: Long,
        mimeType: String?,
        parentId: String?,
        listener: UploadProgressListener
    ) = withContext(Dispatchers.IO) {
        val api = ApiClient.getApi(context)
        val totalChunks = if (fileSize == 0L) 1 else ((fileSize + CHUNK_SIZE - 1) / CHUNK_SIZE).toInt()

        try {
            // 1. Create upload session
            listener.onProgress(0, totalChunks, 0, "در حال ایجاد نشست رمزنگاری در سرور...")
            val sessionReq = CreateUploadRequest(
                parent_id = parentId,
                name = fileName,
                size_bytes = fileSize,
                mime_type = mimeType ?: "application/octet-stream",
                chunk_size = CHUNK_SIZE
            )

            val sessionResp = api.createUploadSession(sessionReq)
            if (!sessionResp.isSuccessful || sessionResp.body() == null) {
                listener.onError("خطا در ایجاد نشست آپلود: ${sessionResp.errorBody()?.string() ?: "نامشخص"}")
                return@withContext
            }

            val session = sessionResp.body()!!
            val uploadId = session.id

            // Whole file SHA-256 hasher
            val wholeFileDigest = MessageDigest.getInstance("SHA-256")
            val chunkBuffer = ByteArray(CHUNK_SIZE)

            // 2. Upload chunk by chunk
            context.contentResolver.openInputStream(fileUri)?.use { inputStream: InputStream ->
                for (chunkIndex in 0 until totalChunks) {
                    val bytesRead = readFully(inputStream, chunkBuffer)
                    if (bytesRead <= 0 && fileSize > 0) break

                    val actualChunk = if (bytesRead == CHUNK_SIZE) chunkBuffer else chunkBuffer.copyOf(bytesRead)

                    // Update whole file hasher
                    wholeFileDigest.update(actualChunk)

                    // Compute SHA-256 for this specific chunk
                    val chunkSha256 = sha256Hex(actualChunk)

                    listener.onProgress(
                        chunkIndex + 1,
                        totalChunks,
                        ((chunkIndex.toFloat() / totalChunks) * 100).toInt(),
                        "در حال ارسال چانک ${chunkIndex + 1} از $totalChunks به تلگرام..."
                    )

                    val octetType = "application/octet-stream".toMediaTypeOrNull()
                    val requestBody = actualChunk.toRequestBody(octetType)

                    val chunkResp = api.uploadChunk(
                        uploadId = uploadId,
                        chunkIndex = chunkIndex,
                        contentType = "application/octet-stream",
                        chunkSha256 = chunkSha256,
                        body = requestBody
                    )

                    if (!chunkResp.isSuccessful) {
                        listener.onError("خطا در ارسال چانک $chunkIndex: ${chunkResp.errorBody()?.string() ?: "خطای سرور"}")
                        return@withContext
                    }
                }
            } ?: run {
                listener.onError("امکان خواندن فایل انتخاب‌شده وجود ندارد")
                return@withContext
            }

            // 3. Complete and seal the file
            listener.onProgress(totalChunks, totalChunks, 99, "در حال اعتبارسنجی نهایی و ثبت درایو...")
            val wholeSha256Hex = bytesToHex(wholeFileDigest.digest())

            val completeResp = api.completeUpload(uploadId, CompleteUploadRequest(sha256 = wholeSha256Hex))
            if (completeResp.isSuccessful && completeResp.body() != null) {
                listener.onProgress(totalChunks, totalChunks, 100, "آپلود با موفقیت انجام شد!")
                listener.onSuccess(completeResp.body()!!.node.id, fileName)
            } else {
                listener.onError("اعتبارسنجی نهایی ناموفق بود: ${completeResp.errorBody()?.string() ?: "خطا"}")
            }

        } catch (e: Exception) {
            listener.onError("خطای استثنا در آپلود: ${e.localizedMessage}")
        }
    }

    private fun readFully(inputStream: InputStream, buffer: ByteArray): Int {
        var totalBytes = 0
        while (totalBytes < buffer.size) {
            val read = inputStream.read(buffer, totalBytes, buffer.size - totalBytes)
            if (read == -1) break
            totalBytes += read
        }
        return totalBytes
    }

    private fun sha256Hex(data: ByteArray): String {
        val digest = MessageDigest.getInstance("SHA-256")
        val hash = digest.digest(data)
        return bytesToHex(hash)
    }

    private fun bytesToHex(bytes: ByteArray): String {
        val sb = StringBuilder(bytes.size * 2)
        for (b in bytes) {
            sb.append(String.format("%02x", b))
        }
        return sb.toString()
    }
}
