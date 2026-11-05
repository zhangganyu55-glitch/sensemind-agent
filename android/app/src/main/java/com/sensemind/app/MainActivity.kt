package com.sensemind.app

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.lifecycle.viewmodel.compose.viewModel
import com.sensemind.app.ui.AppViewModel
import com.sensemind.app.ui.SenseMindApp

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            val viewModel: AppViewModel = viewModel()
            SenseMindApp(viewModel)
        }
    }
}
