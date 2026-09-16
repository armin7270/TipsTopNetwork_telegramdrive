package com.teledrive.app.data.models

import com.google.gson.annotations.SerializedName

data class LoginRequest(
    val email: String,
    val password: String
)

data class RegisterRequest(
    val email: String,
    val password: String,
    @SerializedName("display_name") val displayName: String? = null
)

data class AuthResponse(
    val user: UserDto,
    val tokens: TokenPairDto
)

data class UserDto(
    val id: String,
    val email: String?,
    @SerializedName("display_name") val displayName: String?,
    val role: String,
    @SerializedName("quota_bytes") val quotaBytes: Long,
    @SerializedName("used_bytes") val usedBytes: Long
)

data class TokenPairDto(
    @SerializedName("access_token") val accessToken: String,
    @SerializedName("refresh_token") val refreshToken: String,
    @SerializedName("token_type") val tokenType: String,
    @SerializedName("expires_in") val expiresIn: Long
)

data class UsageResponse(
    @SerializedName("quota_bytes") val quotaBytes: Long,
    @SerializedName("used_bytes") val usedBytes: Long,
    @SerializedName("available_bytes") val availableBytes: Long,
    @SerializedName("utilization_pct") val utilizationPct: Double
)
