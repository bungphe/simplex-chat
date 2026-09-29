# Mẫu chính sách bảo mật và việc cần làm của chủ cửa hàng

Hệ thống nhân viên AI lưu dữ liệu cá nhân của khách (tên, số điện thoại, email, địa chỉ, nội dung
chat, đơn hàng) và gửi nội dung chat tới nhà cung cấp model AI để trả lời. Theo **Nghị định
13/2023/NĐ-CP** về bảo vệ dữ liệu cá nhân, chủ cửa hàng (bên kiểm soát dữ liệu) cần thông báo
cho khách, công bố chính sách và đáp ứng quyền của khách. Mọi thứ dưới đây có sẵn trong trang
quản trị: **Cài đặt hộp thư → Quyền riêng tư** và nút trong trang **Khách hàng**.

## Việc cần làm

- [ ] **Đăng tải chính sách bảo mật.** Sửa mẫu bên dưới cho đúng cửa hàng (tên, địa chỉ, email liên
      hệ, thời hạn lưu) trong *Cài đặt hộp thư → Quyền riêng tư*. Khi web bán hàng được cấu hình
      (`storefront.public_url`), chính sách hiện ở `<địa chỉ web>/privacy`; nếu không có web, đăng
      nội dung này lên fanpage/Zalo OA hoặc in tại quầy.
- [ ] **Thông báo khách đang nói chuyện với AI.** Giữ bật "Thông báo trợ lý AI": câu trả lời đầu
      tiên của AI trong mỗi hội thoại (mọi kênh: SimpleX, Zalo, Facebook, webhook…) mở đầu bằng
      thông báo, dịch sang ngôn ngữ của khách. Nếu bạn dùng model chạy tại chỗ (Ollama, vLLM), sửa
      mục 3 của chính sách cho phù hợp (dữ liệu không rời máy chủ của bạn).
- [ ] **Chọn thời hạn lưu tin nhắn.** "Tự xoá tin nhắn của hội thoại đã đóng sau N ngày" xoá nội
      dung chat cũ (kể cả tệp đính kèm) của các hội thoại đã đóng; đơn hàng và hoá đơn được giữ theo
      luật kế toán. 0 = giữ mãi (khi đó ghi rõ trong chính sách). Ghi cùng số ngày vào mục 4.
- [ ] **Điền email nhận yêu cầu về dữ liệu** và cho nhân viên biết quy trình bên dưới.
- [ ] **Xử lý yêu cầu xem / xoá dữ liệu trong 72 giờ** kể từ khi nhận (Điều 9, 14, 16 Nghị định 13):
      1. Xác minh người yêu cầu đúng là khách (gọi lại số điện thoại đã lưu, hoặc yêu cầu nhắn từ
         chính kênh chat đó).
      2. Trang **Khách hàng** → chọn khách → **Xuất dữ liệu** để gửi bản sao (tệp JSON; bỏ phần
         `internal_notes` nếu không muốn gửi ghi chú nội bộ), hoặc **Xoá dữ liệu khách** rồi gõ lại
         số điện thoại/tên để xác nhận.
      3. Xoá sẽ: xoá hội thoại và tin nhắn trên mọi kênh đã gộp vào khách này, trí nhớ của nhân viên
         AI (lịch sử, tóm tắt, ghi chú, ngôn ngữ), điểm tích luỹ, phiên đăng nhập và mã đăng nhập
         web; **đơn hàng và lịch giao được giữ để làm sổ sách nhưng tên, số điện thoại, địa chỉ,
         email thay bằng "[đã xoá]"**. Việc xoá được ghi vào nhật ký quyền riêng tư (ai, lúc nào,
         bao nhiêu dòng; không ghi lại thông tin đã xoá).
      4. Trả lời khách là đã thực hiện. Khách trên SimpleX cũng có thể tự nhắn `/forget` để AI quên
         cuộc trò chuyện (cửa hàng vẫn giữ tin nhắn và đơn hàng cho đến khi bạn xoá như trên).
- [ ] **Bảo vệ máy chủ:** thư mục `state_dir` (cơ sở dữ liệu, API key) chỉ chủ sở hữu đọc được; giới
      hạn tài khoản quản trị; xem thêm mục "Quyền riêng tư" trong README.
- [ ] Nếu chuyển dữ liệu ra nước ngoài (model AI hoặc nền tảng chat đặt ngoài Việt Nam), cân nhắc hồ
      sơ đánh giá tác động chuyển dữ liệu ra nước ngoài theo Điều 25 Nghị định 13.

Chỗ nào chưa được bao phủ: hàng chờ duyệt (`actions`) và nhật ký chạy (`runlog`) có thể còn tên hoặc
mã khách trong nội dung yêu cầu cũ; email đã gửi nằm trong hộp thư đi của bạn; bản sao trên nền tảng
chat (Zalo, Facebook) do nền tảng đó giữ.

## Mẫu chính sách (mặc định của hệ thống)

`{shop}`, `{address}`, `{phone}`, `{email}`, `{url}`, `{retention}` được thay tự động bằng tên, địa
chỉ, số điện thoại cửa hàng (Cài đặt kho hàng), email liên hệ, địa chỉ trang chính sách và thời hạn
lưu. Dòng bắt đầu bằng `- ` thành gạch đầu dòng, `## ` thành tiêu đề. Câu chứa giá trị còn trống
(chưa có email, chưa có web) tự được bỏ.

```
# Chính sách bảo mật dữ liệu cá nhân của {shop}

{shop} ("chúng tôi") tôn trọng quyền riêng tư của khách hàng. Chính sách này cho biết chúng tôi thu thập, sử dụng, lưu trữ và bảo vệ dữ liệu cá nhân của bạn như thế nào khi bạn nhắn tin, đặt hàng hoặc truy cập website của chúng tôi, theo Nghị định 13/2023/NĐ-CP về bảo vệ dữ liệu cá nhân.

## 1. Dữ liệu chúng tôi thu thập
- Họ tên, số điện thoại, email, địa chỉ giao hàng.
- Nội dung trao đổi với chúng tôi trên các kênh chat (SimpleX, Zalo, Facebook Messenger, website, email…), kể cả ảnh và tệp bạn gửi.
- Đơn hàng, thanh toán, lịch sử mua hàng, điểm tích luỹ.
- Ngôn ngữ bạn dùng và tên hiển thị trên nền tảng chat.

## 2. Mục đích sử dụng
- Tư vấn và trả lời tin nhắn của bạn (bởi nhân viên hoặc trợ lý AI).
- Xử lý đơn hàng, giao hàng, bảo hành, xuất hoá đơn và chăm sóc sau bán.
- Thông báo về đơn hàng; thông báo ưu đãi khi bạn đồng ý nhận.
- Thống kê nội bộ để cải thiện dịch vụ.
Chúng tôi không bán dữ liệu cá nhân của bạn.

## 3. Trợ lý AI và các bên xử lý dữ liệu
Một phần tin nhắn được trợ lý AI trả lời. Khi đó nội dung trao đổi và thông tin cần thiết để trả lời được gửi tới nhà cung cấp mô hình AI mà chúng tôi sử dụng, để xử lý thay mặt chúng tôi. Bạn có thể yêu cầu gặp người thật bất cứ lúc nào bằng cách nhắn "nhân viên".
Dữ liệu cũng đi qua nền tảng chat bạn dùng (Zalo, Meta, Telegram…) theo chính sách của nền tảng đó. Đơn vị vận chuyển nhận tên, số điện thoại và địa chỉ để giao hàng.

## 4. Thời gian lưu trữ
Nội dung chat được lưu {retention}. Dữ liệu đơn hàng và hoá đơn được lưu theo thời hạn của pháp luật về kế toán và thuế.

## 5. Quyền của bạn
- Xem và nhận bản sao dữ liệu cá nhân của mình.
- Sửa thông tin chưa đúng.
- Yêu cầu xoá dữ liệu: chúng tôi xoá nội dung chat, trí nhớ của trợ lý AI và tài khoản mua hàng; đơn hàng được ẩn danh (giữ số liệu kế toán, bỏ tên, số điện thoại, địa chỉ).
- Rút lại sự đồng ý, phản đối hoặc hạn chế việc xử lý dữ liệu.
Chúng tôi trả lời yêu cầu của bạn trong vòng 72 giờ kể từ khi nhận được.

## 6. Bảo mật
Dữ liệu được lưu trên máy chủ do chúng tôi quản lý; chỉ nhân viên được phân quyền mới truy cập. Tin nhắn SimpleX được mã hoá đầu-cuối giữa bạn và tài khoản của cửa hàng.

## 7. Cookie trên website
Website chỉ dùng cookie cần thiết để giữ giỏ hàng và phiên đăng nhập của bạn; không dùng cookie theo dõi quảng cáo.

## 8. Liên hệ
{shop} – {address}
Điện thoại: {phone} · Email: {email}
Chính sách này có thể được cập nhật; bản mới nhất luôn ở {url}.
```

## Thông báo AI (mặc định)

> Bạn đang trò chuyện với trợ lý AI của {shop}. Nhắn 'nhân viên' hoặc /staff để gặp người thật. Chính sách bảo mật: {url}

Gửi một lần cho mỗi hội thoại, ở đầu câu trả lời đầu tiên của AI, bằng ngôn ngữ của khách. Hội
thoại do người trả lời từ đầu không nhận thông báo (không có AI tham gia).
