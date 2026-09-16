package com.teledrive.app.data.models

import com.google.gson.annotations.SerializedName

data class NodeDto(
    val id: String,
    @SerializedName("parent_id") val parentId: String?,
    val kind: String, // "folder" or "file"
    val name: String,
    @SerializedName("size_bytes") val sizeBytes: Long,
    @SerializedName("mime_type") val mimeType: String?,
    val sha256: String?,
    @SerializedName("chunk_size") val chunkSize: Int?,
    @SerializedName("total_chunks") val totalChunks: Int,
    @SerializedName("upload_state") val uploadState: String?,
    @SerializedName("is_starred") val isStarred: Boolean,
    @SerializedName("created_at") val createdAt: String,
    @SerializedName("updated_at") val updatedAt: String
)

data class CreateFolderRequest(
    @SerializedName("parent_id") val parentId: String?,
    val name: String
)

data class RenameNodeRequest(
    val name: String
)
