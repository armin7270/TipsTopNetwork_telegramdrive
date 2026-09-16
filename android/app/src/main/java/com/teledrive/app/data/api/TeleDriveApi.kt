package com.teledrive.app.data.api

import com.teledrive.app.data.models.*
import okhttp3.RequestBody
import okhttp3.ResponseBody
import retrofit2.Response
import retrofit2.http.*

interface TeleDriveApi {

    // --- Authentication ---

    @POST("api/v1/auth/login")
    suspend fun login(@Body request: LoginRequest): Response<AuthResponse>

    @POST("api/v1/auth/register")
    suspend fun register(@Body request: RegisterRequest): Response<AuthResponse>

    @GET("api/v1/auth/me")
    suspend fun getMe(): Response<UserDto>

    @GET("api/v1/me/usage")
    suspend fun getUsage(): Response<UsageResponse>

    // --- Virtual File System (VFS) ---

    @GET("api/v1/nodes/root")
    suspend fun getRootNode(): Response<NodeDto>

    @GET("api/v1/nodes")
    suspend fun listChildren(
        @Query("parent_id") parentId: String?,
        @Query("limit") limit: Int = 100,
        @Query("offset") offset: Int = 0
    ): Response<List<NodeDto>>

    @POST("api/v1/nodes/folders")
    suspend fun createFolder(@Body request: CreateFolderRequest): Response<NodeDto>

    @PATCH("api/v1/nodes/{id}/rename")
    suspend fun renameNode(
        @Path("id") id: String,
        @Body request: RenameNodeRequest
    ): Response<NodeDto>

    @DELETE("api/v1/nodes/{id}")
    suspend fun deleteNode(@Path("id") id: String): Response<Unit>

    // --- Resumable Chunk Uploads ---

    @POST("api/v1/uploads")
    suspend fun createUploadSession(@Body request: CreateUploadRequest): Response<UploadSessionDto>

    @PUT("api/v1/uploads/{id}/chunks/{index}")
    suspend fun uploadChunk(
        @Path("id") uploadId: String,
        @Path("index") chunkIndex: Int,
        @Header("Content-Type") contentType: String = "application/octet-stream",
        @Header("X-Chunk-SHA256") chunkSha256: String,
        @Body body: RequestBody
    ): Response<ChunkReceiptDto>

    @POST("api/v1/uploads/{id}/complete")
    suspend fun completeUpload(
        @Path("id") uploadId: String,
        @Body request: CompleteUploadRequest
    ): Response<CompleteUploadResponse>

    // --- Streaming Download & Range ---

    @Streaming
    @GET("api/v1/files/{id}/content")
    suspend fun streamFile(
        @Path("id") fileId: String,
        @Header("Range") range: String? = null
    ): Response<ResponseBody>

    // --- Healthcheck & Telegram Status ---

    @GET("readyz")
    suspend fun checkHealth(): Response<Map<String, Any>>
}
