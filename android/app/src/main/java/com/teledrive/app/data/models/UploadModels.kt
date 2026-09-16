package com.teledrive.app.data.models

import com.google.gson.annotations.SerializedName

data class CreateUploadRequest(
    @SerializedName("parent_id") val parentId: String?,
    val name: String,
    @SerializedName("size_bytes") val sizeBytes: Long,
    @SerializedName("mime_type") val mimeType: String,
    @SerializedName("chunk_size") val chunkSize: Int
)

data class UploadSessionDto(
    val id: String,
    @SerializedName("node_id") val nodeId: String,
    val name: String,
    @SerializedName("size_bytes") val sizeBytes: Long,
    @SerializedName("chunk_size") val chunkSize: Int,
    @SerializedName("total_chunks") val totalChunks: Int,
    val status: String,
    @SerializedName("uploaded_chunks") val uploadedChunks: Int,
    @SerializedName("missing_chunks") val missingChunks: List<Int>?
)

data class ChunkReceiptDto(
    @SerializedName("upload_id") val uploadId: String,
    @SerializedName("chunk_index") val chunkIndex: Int,
    @SerializedName("size_bytes") val sizeBytes: Long,
    val sha256: String,
    val status: String,
    @SerializedName("uploaded_chunks") val uploadedChunks: Int,
    @SerializedName("total_chunks") val totalChunks: Int,
    val complete: Boolean
)

data class CompleteUploadRequest(
    val sha256: String? = null
)

data class CompleteUploadResponse(
    val node: NodeDto,
    val verified: Boolean
)
