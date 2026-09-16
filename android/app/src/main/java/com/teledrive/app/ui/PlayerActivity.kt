package com.teledrive.app.ui

import android.net.Uri
import android.os.Bundle
import android.view.View
import android.widget.ImageButton
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.ui.PlayerView
import com.teledrive.app.R
import com.teledrive.app.data.api.ApiClient

class PlayerActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_FILE_ID = "extra_file_id"
        const val EXTRA_FILE_NAME = "extra_file_name"
    }

    private var player: ExoPlayer? = null
    private lateinit var playerView: PlayerView
    private lateinit var tvMediaTitle: TextView
    private lateinit var btnBack: ImageButton
    private lateinit var pbLoading: ProgressBar

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_player)

        playerView = findViewById(R.id.playerView)
        tvMediaTitle = findViewById(R.id.tvMediaTitle)
        btnBack = findViewById(R.id.btnBack)
        pbLoading = findViewById(R.id.pbLoading)

        val fileId = intent.getStringExtra(EXTRA_FILE_ID) ?: run {
            Toast.makeText(this, "شناسه فایل نامعتبر است", Toast.LENGTH_SHORT).show()
            finish()
            return
        }

        val fileName = intent.getStringExtra(EXTRA_FILE_NAME) ?: "پخش مدیا"
        tvMediaTitle.text = fileName

        btnBack.setOnClickListener { finish() }

        initializePlayer(fileId)
    }

    private fun initializePlayer(fileId: String) {
        val serverUrl = ApiClient.getServerUrl(this)
        val streamUrl = "${serverUrl}api/v1/files/$fileId/content"
        val token = ApiClient.getAccessToken(this)

        val httpDataSourceFactory = DefaultHttpDataSource.Factory()
            .setAllowCrossProtocolRedirects(true)
            .setConnectTimeoutMs(30_000)
            .setReadTimeoutMs(60_000)

        if (!token.isNullOrBlank()) {
            httpDataSourceFactory.setDefaultRequestProperties(
                mapOf("Authorization" to "Bearer $token")
            )
        }

        val mediaSourceFactory = DefaultMediaSourceFactory(httpDataSourceFactory)

        player = ExoPlayer.Builder(this)
            .setMediaSourceFactory(mediaSourceFactory)
            .build()
            .also { exoPlayer ->
                playerView.player = exoPlayer
                val mediaItem = MediaItem.fromUri(Uri.parse(streamUrl))
                exoPlayer.setMediaItem(mediaItem)

                exoPlayer.addListener(object : Player.Listener {
                    override fun onPlaybackStateChanged(playbackState: Int) {
                        when (playbackState) {
                            Player.STATE_BUFFERING -> pbLoading.visibility = View.VISIBLE
                            Player.STATE_READY -> pbLoading.visibility = View.GONE
                            Player.STATE_ENDED -> pbLoading.visibility = View.GONE
                            Player.STATE_IDLE -> pbLoading.visibility = View.GONE
                        }
                    }

                    override fun onPlayerError(error: PlaybackException) {
                        pbLoading.visibility = View.GONE
                        Toast.makeText(
                            this@PlayerActivity,
                            "خطای پخش مدیا: ${error.localizedMessage}",
                            Toast.LENGTH_LONG
                        ).show()
                    }
                })

                exoPlayer.prepare()
                exoPlayer.playWhenReady = true
            }
    }

    override fun onStop() {
        super.onStop()
        player?.pause()
    }

    override fun onDestroy() {
        super.onDestroy()
        player?.release()
        player = null
    }
}
