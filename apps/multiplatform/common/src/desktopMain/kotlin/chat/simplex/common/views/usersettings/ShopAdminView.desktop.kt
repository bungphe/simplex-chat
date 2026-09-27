package chat.simplex.common.views.usersettings

import androidx.compose.runtime.*
import androidx.compose.ui.platform.LocalUriHandler
import chat.simplex.common.views.helpers.openUriCatching

@Composable
actual fun ShopAdminWebView(url: String, close: () -> Unit) {
  // no embedded web view on desktop: the system browser opens the admin app
  val uriHandler = LocalUriHandler.current
  LaunchedEffect(url) {
    uriHandler.openUriCatching(url)
    close()
  }
}
