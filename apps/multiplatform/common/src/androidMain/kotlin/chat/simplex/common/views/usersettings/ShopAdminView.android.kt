package chat.simplex.common.views.usersettings

import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.Context
import android.graphics.Bitmap
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.view.View
import android.view.ViewGroup
import android.webkit.*
import androidx.compose.foundation.layout.*
import androidx.compose.material.LinearProgressIndicator
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.viewinterop.AndroidView
import chat.simplex.common.helpers.applyAppLocale
import chat.simplex.common.model.ChatController.appPrefs
import chat.simplex.common.platform.*
import chat.simplex.common.views.helpers.*
import chat.simplex.res.MR

@SuppressLint("SetJavaScriptEnabled")
@Composable
actual fun ShopAdminWebView(url: String, close: () -> Unit) {
  val uriHandler = LocalUriHandler.current
  val home = remember(url) { Uri.parse(url) }
  var webView by remember { mutableStateOf<WebView?>(null) }
  var canGoBack by remember { mutableStateOf(false) }
  var loading by remember { mutableStateOf(true) }
  // Android back: back in the admin app first, then close the screen
  BackHandler(enabled = canGoBack) { webView?.goBack() }
  Box(Modifier.fillMaxSize()) {
    AndroidView(
      modifier = Modifier.fillMaxSize(),
      factory = {
        try {
          WebView(androidAppContext).apply {
            reapplyLocale()
            layoutParams = ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.allowFileAccess = false
            settings.allowContentAccess = false
            settings.mediaPlaybackRequiresUserGesture = true
            CookieManager.getInstance().setAcceptCookie(true) // the staff session
            webViewClient = object : WebViewClient() {
              override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val target = request.url
                // same site, also target=_blank links (printed receipts): inside the web view, with the session
                if (sameOrigin(target, home)) return false
                // other sites (links in messages, maps): in the browser, never inside the admin app
                uriHandler.openUriCatching(target.toString())
                return true
              }

              override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) {
                loading = true
              }

              override fun onPageFinished(view: WebView, url: String?) {
                loading = false
                canGoBack = view.canGoBack()
              }

              override fun doUpdateVisitedHistory(view: WebView, url: String?, isReload: Boolean) {
                canGoBack = view.canGoBack()
              }
            }
            // CSV exports need the staff session cookie, which the browser does not have:
            // the system download manager fetches them with it (into Downloads, no permission needed on Android 10+)
            setDownloadListener { downloadUrl, userAgent, contentDisposition, mimeType, _ ->
              val target = Uri.parse(downloadUrl)
              if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && sameOrigin(target, home)) {
                downloadWithSession(downloadUrl, userAgent, contentDisposition, mimeType)
              } else {
                uriHandler.openUriCatching(downloadUrl)
              }
            }
            loadUrl(url)
            webView = this
          }
        } catch (e: Exception) {
          Log.e(TAG, "ShopAdminWebView: ${e.stackTraceToString()}")
          AlertManager.shared.showAlertMsg(generalGetString(MR.strings.error), generalGetString(MR.strings.error_initializing_web_view).format(e.stackTraceToString()))
          View(androidAppContext)
        }
      }
    )
    if (loading) LinearProgressIndicator(Modifier.fillMaxWidth().align(androidx.compose.ui.Alignment.TopCenter))
  }
  DisposableEffect(Unit) {
    onDispose {
      webView?.destroy()
      webView = null
    }
  }
}

private fun sameOrigin(a: Uri, b: Uri): Boolean =
  a.scheme == b.scheme && a.host == b.host && a.port == b.port

private fun downloadWithSession(url: String, userAgent: String?, contentDisposition: String?, mimeType: String?) {
  try {
    val name = URLUtil.guessFileName(url, contentDisposition, mimeType)
    val request = DownloadManager.Request(Uri.parse(url))
      .setTitle(name)
      .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
      .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name)
    if (!mimeType.isNullOrBlank()) request.setMimeType(mimeType)
    CookieManager.getInstance().getCookie(url)?.let { request.addRequestHeader("Cookie", it) }
    if (!userAgent.isNullOrBlank()) request.addRequestHeader("User-Agent", userAgent)
    (androidAppContext.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager).enqueue(request)
  } catch (e: Exception) {
    Log.e(TAG, "ShopAdminWebView download: ${e.stackTraceToString()}")
    AlertManager.shared.showAlertMsg(generalGetString(MR.strings.error), e.message)
  }
}

/*
* Creating a WebView drops the app's own language (https://issuetracker.google.com/issues/109833940)
* */
private fun reapplyLocale() {
  mainActivity.get()?.applyAppLocale(appPrefs.appLanguage)
}
