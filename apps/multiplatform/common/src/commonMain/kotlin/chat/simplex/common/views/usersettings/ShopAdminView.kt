package chat.simplex.common.views.usersettings

import SectionBottomSpacer
import SectionDividerSpaced
import SectionTextFooter
import SectionView
import androidx.compose.foundation.layout.*
import androidx.compose.material.MaterialTheme
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.TextFieldValue
import chat.simplex.common.model.ChatController.appPrefs
import chat.simplex.common.platform.*
import chat.simplex.common.ui.theme.DEFAULT_PADDING
import chat.simplex.common.views.helpers.*
import chat.simplex.res.MR
import dev.icerock.moko.resources.compose.painterResource
import dev.icerock.moko.resources.compose.stringResource

/*
* Shop management: the shop's admin web app (inbox, point of sale, stock, deliveries,
* reports of the SimpleX AI employees) inside SimpleX. Staff sign in there with their
* staff account; the page itself stays on the shop's server.
* */

// https only: the app does not allow cleartext traffic (a back-office server goes through
// the shop's HTTPS reverse proxy or tunnel)
fun validShopAdminUrl(url: String): Boolean {
  val u = url.trim()
  return u.startsWith("https://") && u.length > "https://".length && !u.contains(' ')
}

fun openShopAdmin() {
  val url = appPrefs.shopAdminUrl.get()
  if (url.isNullOrBlank() || !validShopAdminUrl(url)) {
    ModalManager.start.showModalCloseable { close -> ShopAdminView(close) }
  } else {
    ModalManager.fullscreen.showModalCloseable { close -> ShopAdminWebView(url.trim(), close) }
  }
}

@Composable
fun ShopAdminView(close: () -> Unit) {
  val saved = remember { appPrefs.shopAdminUrl.state }
  val state = remember { mutableStateOf(TextFieldValue(saved.value ?: "")) }
  ColumnWithScrollBar {
    AppBarTitle(stringResource(MR.strings.shop_admin))
    SectionView(stringResource(MR.strings.shop_admin_address).uppercase()) {
      DefaultConfigurableTextField(
        state = state,
        placeholder = "https://admin.shop.vn",
        modifier = Modifier.fillMaxWidth().padding(start = DEFAULT_PADDING),
        isValid = { it.isBlank() || validShopAdminUrl(it) },
        keyboardType = KeyboardType.Uri,
      )
    }
    SectionTextFooter(stringResource(MR.strings.shop_admin_address_footer))
    KeyChangeEffect(state.value) {
      val text = state.value.text.trim()
      if (text.isEmpty()) appPrefs.shopAdminUrl.set(null)
      else if (validShopAdminUrl(text)) appPrefs.shopAdminUrl.set(text)
    }
    SectionDividerSpaced()
    SectionView {
      SettingsActionItem(
        painterResource(MR.images.ic_storefront),
        stringResource(MR.strings.shop_admin_open),
        click = {
          close()
          openShopAdmin()
        },
        textColor = MaterialTheme.colors.primary,
        disabled = saved.value.isNullOrBlank(),
      )
    }
    SectionTextFooter(stringResource(MR.strings.shop_admin_chat_footer))
    SectionBottomSpacer()
  }
}

/** The admin web app: in an embedded web view where the platform has one, else the browser. */
@Composable
expect fun ShopAdminWebView(url: String, close: () -> Unit)
