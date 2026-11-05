package com.sensemind.app.data

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * Bearer tokens are stored in Android Keystore-backed preferences when
 * available. The fallback private preferences file is only used if the
 * Keystore implementation cannot be initialized on a test device.
 */
class SessionStore(context: Context) {
    private val prefs: SharedPreferences = try {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        EncryptedSharedPreferences.create(
            context,
            "sensemind_session_encrypted",
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    } catch (_: Exception) {
        context.getSharedPreferences("sensemind_session_fallback", Context.MODE_PRIVATE)
    }

    fun load(): Session? {
        val token = prefs.getString(KEY_TOKEN, null) ?: return null
        if (token.isBlank()) return null
        val demo = prefs.getString(KEY_DEMO, null)?.let { value ->
            DemoMode.entries.firstOrNull { it.wire == value }
        }
        return Session(
            token = token,
            guardianId = prefs.getString(KEY_GUARDIAN, "").orEmpty(),
            familyId = prefs.getString(KEY_FAMILY, "").orEmpty(),
            name = prefs.getString(KEY_NAME, "").orEmpty(),
            role = Role.parse(prefs.getString(KEY_ROLE, "legacy")),
            baseUrl = prefs.getString(KEY_BASE, "").orEmpty(),
            demoMode = demo,
        )
    }

    fun save(session: Session) {
        prefs.edit()
            .putString(KEY_TOKEN, session.token)
            .putString(KEY_GUARDIAN, session.guardianId)
            .putString(KEY_FAMILY, session.familyId)
            .putString(KEY_NAME, session.name)
            .putString(KEY_ROLE, session.role.name.lowercase())
            .putString(KEY_BASE, session.baseUrl)
            .putString(KEY_DEMO, session.demoMode?.wire)
            .apply()
    }

    fun clear() {
        prefs.edit().clear().apply()
    }

    private companion object {
        const val KEY_TOKEN = "token"
        const val KEY_GUARDIAN = "guardian_id"
        const val KEY_FAMILY = "family_id"
        const val KEY_NAME = "name"
        const val KEY_ROLE = "role"
        const val KEY_BASE = "base_url"
        const val KEY_DEMO = "demo_mode"
    }
}
