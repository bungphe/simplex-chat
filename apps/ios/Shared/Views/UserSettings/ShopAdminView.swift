//
//  ShopAdminView.swift
//  SimpleX (iOS)
//
//  Shop management: the admin web app of the shop's SimpleX AI employees (inbox, point
//  of sale, stock, deliveries, reports) inside SimpleX. Staff sign in there with their
//  staff account; the page stays on the shop's server.
//

import SwiftUI
import WebKit
import SimpleXChat

let DEFAULT_SHOP_ADMIN_URL = "shopAdminUrl"

/// https only: App Transport Security blocks cleartext http (a back-office server goes
/// through the shop's HTTPS reverse proxy or tunnel)
func validShopAdminUrl(_ s: String) -> Bool {
    let u = s.trimmingCharacters(in: .whitespaces)
    return u.hasPrefix("https://") && u.count > 8 && !u.contains(" ") && URL(string: u) != nil
}

/// From a chat: the admin app when its address is saved, else the settings to enter it.
struct ShopAdminEntry: View {
    @AppStorage(DEFAULT_SHOP_ADMIN_URL) private var shopAdminUrl = ""

    var body: some View {
        if validShopAdminUrl(shopAdminUrl), let url = URL(string: shopAdminUrl.trimmingCharacters(in: .whitespaces)) {
            // a new address (edited from the web screen) reloads the web view
            ShopAdminWebScreen(url: url).id(url)
        } else {
            ShopAdminSettings()
        }
    }
}

struct ShopAdminSettings: View {
    @EnvironmentObject var theme: AppTheme
    @AppStorage(DEFAULT_SHOP_ADMIN_URL) private var shopAdminUrl = ""
    @State private var address = ""
    /// false when opened from the admin app itself (back returns to it)
    var showOpen = true

    var body: some View {
        List {
            Section {
                TextField("https://admin.shop.com", text: $address)
                    .keyboardType(.URL)
                    .autocapitalization(.none)
                    .disableAutocorrection(true)
                    .foregroundColor(address.isEmpty || validShopAdminUrl(address) ? theme.colors.onBackground : .red)
                    .onSubmit { save() }
                // saved only when done typing: a partial address is not a new admin app
                Button("Save") { save() }
                    .disabled(!canSave)
            } header: {
                Text("Admin app address").foregroundColor(theme.colors.secondary)
            } footer: {
                Text("The address of your shop's admin web app (inbox, point of sale, stock, deliveries, reports), e.g. https://admin.shop.com. You sign in there with your staff account.")
                    .foregroundColor(theme.colors.secondary)
            }

            Section {
                if showOpen, validShopAdminUrl(shopAdminUrl), let url = URL(string: shopAdminUrl) {
                    NavigationLink {
                        ShopAdminWebScreen(url: url, canEdit: false)
                    } label: {
                        settingsRow("bag", color: theme.colors.primary) { Text("Open shop management").foregroundColor(theme.colors.primary) }
                    }
                }
            } footer: {
                Text("In the chat with the shop's AI employee, type / to see your commands. First link your chat to your staff account: in the admin app, open your account, get a link code and send /link with the code.")
                    .foregroundColor(theme.colors.secondary)
            }
        }
        .onAppear { address = shopAdminUrl }
        .navigationTitle("Shop management")
        .modifier(ThemedBackground(grouped: true))
    }

    private var canSave: Bool {
        let text = address.trimmingCharacters(in: .whitespaces)
        return text != shopAdminUrl && (text.isEmpty || validShopAdminUrl(text))
    }

    private func save() {
        if canSave { shopAdminUrl = address.trimmingCharacters(in: .whitespaces) }
    }
}

struct ShopAdminWebScreen: View {
    let url: URL
    /// the address can be edited from here (false when opened from its settings)
    var canEdit = true
    @State private var loading = true
    @State private var showSettings = false

    var body: some View {
        ZStack(alignment: .top) {
            NavigationLink(isActive: $showSettings) {
                ShopAdminSettings(showOpen: false)
            } label: {
                EmptyView()
            }
            .frame(width: 1, height: 1)
            .hidden()
            ShopAdminWebView(url: url, loading: $loading)
            if loading { ProgressView().padding() }
        }
        .navigationTitle("Shop management")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .navigationBarTrailing) {
                if canEdit {
                    Button {
                        showSettings = true
                    } label: {
                        Image(systemName: "gearshape")
                    }
                    .accessibilityLabel(Text("Admin app address"))
                }
            }
        }
    }
}

struct ShopAdminWebView: UIViewRepresentable {
    let url: URL
    @Binding var loading: Bool

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default() // keeps the staff session
        let view = WKWebView(frame: .zero, configuration: config)
        view.allowsBackForwardNavigationGestures = true
        view.navigationDelegate = context.coordinator
        view.uiDelegate = context.coordinator
        view.load(URLRequest(url: url))
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) {}

    func makeCoordinator() -> Coordinator {
        Coordinator(home: url, loading: $loading)
    }

    class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
        let home: URL
        @Binding var loading: Bool
        private var downloads: [WKDownload: URL] = [:]

        init(home: URL, loading: Binding<Bool>) {
            self.home = home
            self._loading = loading
        }

        private func sameSite(_ url: URL) -> Bool {
            url.scheme == home.scheme && url.host == home.host && url.port == home.port
        }

        func webView(_ webView: WKWebView,
                     decidePolicyFor navigationAction: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let url = navigationAction.request.url else { return decisionHandler(.allow) }
            if sameSite(url) { return decisionHandler(.allow) }
            // other sites (links in messages, maps): in Safari, never inside the admin app
            decisionHandler(.cancel)
            UIApplication.shared.open(url)
        }

        // links opening a new window: the admin app's own pages (receipts to print) here, with the
        // staff session (Safari does not have it); other sites in Safari
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = navigationAction.request.url {
                if sameSite(url) {
                    webView.load(navigationAction.request)
                } else {
                    UIApplication.shared.open(url)
                }
            }
            return nil
        }

        // files (CSV exports) are downloaded with the staff session, then shared/saved from the share sheet
        func webView(_ webView: WKWebView,
                     decidePolicyFor navigationResponse: WKNavigationResponse,
                     decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
            let disposition = (navigationResponse.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
            if disposition.lowercased().hasPrefix("attachment") || !navigationResponse.canShowMIMEType {
                decisionHandler(.download)
            } else {
                decisionHandler(.allow)
            }
        }

        func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
            download.delegate = self
        }

        func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                      suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
            let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
            do {
                try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
                let file = dir.appendingPathComponent(suggestedFilename)
                downloads[download] = file
                completionHandler(file)
            } catch {
                logger.error("ShopAdminWebView download: \(error.localizedDescription)")
                completionHandler(nil)
            }
        }

        func downloadDidFinish(_ download: WKDownload) {
            loading = false
            if let file = downloads.removeValue(forKey: download) {
                showShareSheet(items: [file])
            }
        }

        func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
            loading = false
            downloads.removeValue(forKey: download)
            logger.error("ShopAdminWebView download: \(error.localizedDescription)")
        }

        func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
            loading = true
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            loading = false
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            loading = false
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            loading = false
        }
    }
}
