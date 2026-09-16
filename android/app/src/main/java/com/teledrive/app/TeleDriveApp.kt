package com.teledrive.app

import android.app.Application

class TeleDriveApp : Application() {

    override fun onCreate() {
        super.onCreate()
        instance = this
    }

    companion object {
        lateinit var instance: TeleDriveApp
            private set
    }
}
