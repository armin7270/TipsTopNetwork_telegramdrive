# Add project specific ProGuard rules here.
-keep class com.teledrive.app.data.models.** { *; }
-keepclassmembers class * {
    @com.google.gson.annotations.SerializedName <fields>;
}
