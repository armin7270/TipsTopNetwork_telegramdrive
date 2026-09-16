package com.teledrive.app.ui

import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ImageButton
import android.widget.TextView
import androidx.recyclerview.widget.RecyclerView
import com.teledrive.app.R
import com.teledrive.app.data.models.NodeDto
import java.util.Locale

class FileAdapter(
    private val onItemClick: (NodeDto) -> Unit,
    private val onMenuClick: (NodeDto, View) -> Unit
) : RecyclerView.Adapter<FileAdapter.ViewHolder>() {

    private val items = mutableListOf<NodeDto>()

    fun submitList(newItems: List<NodeDto>) {
        items.clear()
        // Sort: folders first, then files alphabetically
        items.addAll(newItems.sortedWith(compareBy({ it.kind != "folder" }, { it.name.lowercase(Locale.ROOT) })))
        notifyDataSetChanged()
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ViewHolder {
        val view = LayoutInflater.from(parent.context).inflate(R.layout.item_file_node, parent, false)
        return ViewHolder(view)
    }

    override fun onBindViewHolder(holder: ViewHolder, position: Int) {
        holder.bind(items[position])
    }

    override fun getItemCount(): Int = items.size

    inner class ViewHolder(itemView: View) : RecyclerView.ViewHolder(itemView) {
        private val tvEmoji: TextView = itemView.findViewById(R.id.tvFileEmoji)
        private val tvName: TextView = itemView.findViewById(R.id.tvNodeName)
        private val tvInfo: TextView = itemView.findViewById(R.id.tvNodeInfo)
        private val tvStatusBadge: TextView = itemView.findViewById(R.id.tvStatusBadge)
        private val btnMenu: ImageButton = itemView.findViewById(R.id.btnMenu)

        fun bind(node: NodeDto) {
            tvName.text = node.name

            if (node.kind == "folder") {
                tvEmoji.text = "📁"
                tvInfo.text = "پوشه"
                tvStatusBadge.visibility = View.GONE
            } else {
                tvEmoji.text = getFileEmoji(node.name, node.mimeType)
                val sizeFormatted = formatFileSize(node.sizeBytes)
                val datePart = node.createdAt.take(10)
                tvInfo.text = "$sizeFormatted • $datePart"

                if (node.uploadState == "completed") {
                    tvStatusBadge.visibility = View.VISIBLE
                    tvStatusBadge.text = "تلگرام (${node.totalChunks} بخش)"
                } else {
                    tvStatusBadge.visibility = View.GONE
                }
            }

            itemView.setOnClickListener { onItemClick(node) }
            btnMenu.setOnClickListener { onMenuClick(node, it) }
        }

        private fun getFileEmoji(name: String, mime: String?): String {
            val ext = name.substringAfterLast('.', "").lowercase()
            return when {
                ext in listOf("mp4", "mkv", "avi", "mov", "webm") || (mime?.startsWith("video/") == true) -> "🎬"
                ext in listOf("mp3", "wav", "flac", "ogg", "m4a") || (mime?.startsWith("audio/") == true) -> "🎵"
                ext in listOf("jpg", "jpeg", "png", "webp", "gif", "svg") || (mime?.startsWith("image/") == true) -> "🖼️"
                ext in listOf("pdf") -> "📄"
                ext in listOf("zip", "rar", "7z", "tar", "gz") -> "📦"
                ext in listOf("doc", "docx", "txt", "md") -> "📝"
                ext in listOf("xls", "xlsx", "csv") -> "📊"
                ext in listOf("apk") -> "🤖"
                else -> "📄"
            }
        }

        private fun formatFileSize(bytes: Long): String {
            if (bytes <= 0) return "0 B"
            val units = arrayOf("B", "KB", "MB", "GB", "TB")
            val digitGroups = (Math.log10(bytes.toDouble()) / Math.log10(1024.0)).toInt().coerceIn(0, 4)
            return String.format(Locale.US, "%.1f %s", bytes / Math.pow(1024.0, digitGroups.toDouble()), units[digitGroups])
        }
    }
}
