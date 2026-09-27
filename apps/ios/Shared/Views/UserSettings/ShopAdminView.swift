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

/// https, or http on this device / the local network (a shop server in the back office)
func validShopAdminUrl(_ s: String) -> Bool {
    let u = s.trimmingCharacters(in: .whitespaces)
    if u.hasPrefix("https://") && u.count > 8 && !u.contains(" ") && URL(string: u) != nil { return true }
    return u.range(
        of: #"^http://(localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)(:\d+)?(/.*)?$"#,
        options: .regularExpression
    ) != nil
}

/// Settings when there is no address yet, else the admin app itself.
struct ShopAdminEntry: View {
    @AppStorage(DEFAULT_SHOP_ADMIN_URL) private var shopAdminUrl = ""

    var body: some View {
        if validShopAdminUrl(shopAdminUrl), let url = URL(string: shopAdminUrl.trimmingCharacters(in: .whitespaces)) {
            ShopAdminWebScreen(url: url)
        } else {
            ShopAdminSettings()
                .navigationTitle("Shop management")
                .modifier(ThemedBackground(grouped: true))
        }
    }
}

struct ShopAdminSettings: View {
    @EnvironmentObject var theme: AppTheme
    @AppStorage(DEFAULT_SHOP_ADMIN_URL) private var shopAdminUrl = ""
    @State private var address = ""

    var body: some View {
        List {
            Section {
                TextField("https://admin.shop.com", text: $address)
                    .keyboardType(.URL)
                    .autocapitalization(.none)
                    .disableAutocorrection(true)
                    .foregroundColor(address.isEmpty || validShopAdminUrl(address) ? theme.colors.onBackground : .red)
                    .onChange(of: address) { a in
                        let text = a.trimmingCharacters(in: .whitespaces)
                        if text.isEmpty || validShopAdminUrl(text) { shopAdminUrl = text }
                    }
            } header: {
                Text("Admin app address").foregroundColor(theme.colors.secondary)
            } footer: {
                Text("The address of your shop's admin web app (inbox, point of sale, stock, deliveries, reports), e.g. https://admin.shop.com. You sign in there with your staff account.")
                    .foregroundColor(theme.colors.secondary)
            }

            Section {
                if validShopAdminUrl(shopAdminUrl), let url = URL(string: shopAdminUrl) {
                    NavigationLink {
                        ShopAdminWebScreen(url: url)
                    } label: {
                        settingsRow("bag", color: theme.colors.primary) { Text("Open shop management").foregroundColor(theme.colors.primary) }
                    }
                }
            } footer: {
                Text("In the chat with the shop's AI employee, type / for your commands. Link your chat to your staff account first: in the admin app, Account → Link SimpleX, then send /link and the code.")
                    .foregroundColor(theme.colors.secondary)
            }
        }
        .onAppear { address = shopAdminUrl }
    }
}

struct ShopAdminWebScreen: View {
    let url: URL
    @State private var loading = true

    var body: some View {
        ZStack(alignment: .top) {
            ShopAdminWebView(url: url, loading: $loading)
            if loading { ProgressView().padding() }
        }
        .navigationTitle("Shop management")
        .navigationBarTitleDisplayMode(.inline)
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

    class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        let home: URL
        @Binding var loading: Bool

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

        // links opening a new window (receipts to print, CSV exports): in Safari
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = navigationAction.request.url { UIApplication.shared.open(url) }
            return nil
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
