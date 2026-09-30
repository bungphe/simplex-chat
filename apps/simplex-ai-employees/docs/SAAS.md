# Control plane SaaS: nhiều cửa hàng, mỗi cửa hàng một hệ thống riêng

`python -m ai_employees saas --config saas.yaml` chạy một dịch vụ riêng (gói `ai_employees.saas`) đứng trước
sản phẩm: nhận đăng ký dùng thử, tự khởi tạo cho mỗi khách thuê (tenant) một bản chạy riêng của ứng dụng nhân viên AI,
xuất hoá đơn hằng tháng, tạm dừng và xoá theo quy trình, và có cổng tự phục vụ cho khách lẫn bảng điều hành cho
người vận hành. Sản phẩm không đổi: mỗi khách vẫn là một `employees.yaml` + một container như hướng dẫn trong README.

## Kiến trúc

```
                Internet (HTTPS)
                      │
              ┌───────▼────────┐  on-demand TLS (hoặc wildcard) cho *.aie.example.vn
              │     Caddy      │  saas.example.vn  → 127.0.0.1:8090  (control plane)
              │  (network host)│  <slug>.aie…      → 127.0.0.1:20000+2*id   (trang quản trị của khách)
              └──┬──────┬──────┘  <slug>-shop.aie… → 127.0.0.1:20001+2*id   (website bán hàng của khách)
                 │      │
   ┌─────────────▼┐   ┌─▼──────────────────────────────────────────────────┐
   │ control plane│   │ /srv/aie/tenants/<slug>/                           │
   │ :8090        │   │   data/employees.yaml  (sinh từ mẫu, có limits:)   │
   │ saas.sqlite  │──►│   .env                 (AI_ADMIN_PASSWORD, API key)│  docker compose up -d
   │ (hoặc PG)    │   │   docker-compose.yml   (image ghim, cổng 127.0.0.1)│
   └──────────────┘   │   data/…               (SimpleX, kho hàng, trạng thái)│
                      └────────────────────────────────────────────────────┘
```

- **Một container mỗi khách.** Control plane sinh `docker-compose.yml` cho từng khách từ mẫu trong gói
  (`provisioner.py`), ghim `image:` đã cấu hình, mount thư mục `data/` và mở cổng chỉ trên `127.0.0.1`. Cổng được
  cấp theo id khách (`port_base + 2*id`) và lưu trong bảng `tenants`. Tất cả lệnh shell đi qua một hàm `run()`
  duy nhất, tham số không qua shell, và slug phải khớp `^[a-z0-9][a-z0-9-]{2,30}$` trước khi vào đường dẫn hay lệnh.
- **Caddy tự cấp TLS.** Mỗi khách có một tệp `<caddy_dir>/<slug>.caddy` (matcher theo host + `reverse_proxy`),
  được `import` trong khối site wildcard của Caddyfile; sau mỗi thay đổi control plane chạy lệnh `caddy_reload`.
  Chứng chỉ cấp **on-demand** (Caddy hỏi `GET /caddy-ask?domain=` của control plane trước khi xin chứng chỉ, chỉ
  host của khách đang hoạt động mới được cấp) hoặc **wildcard** qua DNS challenge nếu bạn build Caddy kèm module
  DNS của nhà cung cấp tên miền.
- **Cơ sở dữ liệu riêng** (`Database` của `db.py`: SQLite hoặc PostgreSQL): `tenants`, `email_codes`, `invoices`,
  `events` (nhật ký mọi thao tác), `saas_sessions`. Mật khẩu băm scrypt có muối; token phiên và mã xác nhận chỉ
  lưu dạng băm. Mật khẩu quản trị của khách **không lưu**: sinh lúc khởi tạo, ghi vào `.env` của khách, hiển thị
  một lần; "Cấp lại mật khẩu quản trị" sinh mật khẩu mới, ghi lại `.env` và khởi động lại container.
- **Giới hạn gói nằm trong sản phẩm.** Provisioner ghi `limits: {users, employees, channels, storage_mb}` vào
  `employees.yaml` của khách. `config.py` từ chối cấu hình vượt số nhân viên AI hoặc kênh; `Users.add` từ chối
  tài khoản nhân viên thứ N+1 ("Gói dịch vụ của bạn cho phép tối đa {0} tài khoản nhân viên").

### Vòng đời khách thuê

| Trạng thái | Vào khi | Ra khi |
|---|---|---|
| `pending_email` | gửi form đăng ký (giữ slug và email tối đa 1 ngày) | nhập đúng mã 6 số → `trial`; quá hạn → xoá dòng |
| `trial` | xác nhận email; khởi tạo chạy nền (3 lần thử) | hết `trial_days`: đã trả hoá đơn đầu → `active`, chưa → `suspended` |
| `active` | đã thanh toán đến `paid_until` | quá `paid_until` chưa trả → `past_due` |
| `past_due` | quá hạn | trả → `active`; quá thêm `grace_days` → `suspended` (container dừng) |
| `suspended` | chưa thanh toán, hoặc người vận hành tạm dừng | trả hoá đơn / "Mở lại" → `active`; quá `delete_after_days` → `deleted` |
| `deleted` | xoá (lưu trữ `data/` thành tar.gz trong `backups_dir` trước) | slug và email được giải phóng để đăng ký lại |

Công việc hằng ngày (`billing.daily`, chạy lúc 3:00 trong tiến trình, hoặc `--daily` cho cron): nhắc dùng thử ở
ngày 10 và 13 (kèm hoá đơn đầu tiên để khách trả trước khi hết hạn), xử lý hết hạn dùng thử, xuất hoá đơn tháng
kế tiếp 7 ngày trước khi kỳ đã trả kết thúc (mỗi kỳ một hoá đơn, chạy lại không tạo trùng), đánh dấu quá hạn, tạm
dừng, xoá; mỗi bước gửi email (tiếng Việt/tiếng Anh theo ngôn ngữ khách chọn). Khách đã "Yêu cầu ngừng dịch vụ"
không nhận hoá đơn mới và dừng khi hết kỳ đã trả.

## Cài đặt

1. **DNS.** `saas.example.vn` (control plane) và bản ghi wildcard `*.aie.example.vn` trỏ về máy chủ.
2. **Image sản phẩm** đẩy lên registry (hoặc build tại chỗ) và ghim trong `saas.yaml` (`image:`); build image
   control plane từ đó: `docker build -f Dockerfile.saas -t simplex-ai-employees-saas .` (thêm Docker CLI, chạy
   root vì cần Docker socket).
3. **`saas.yaml`**: chép `src/ai_employees/saas/saas.example.yaml`, sửa `public_url`, `base_domain`, `image`, gói
   giá, `operators`, `smtp`, `bank`, `tenant_env`/`tenant_model` (API key của nền tảng cấp cho khách qua `${TÊN}`).
4. **Caddyfile** (`/srv/aie/caddy/Caddyfile`):

   ```caddyfile
   {
       email admin@example.vn
       on_demand_tls {
           ask http://127.0.0.1:8090/caddy-ask
       }
   }
   saas.example.vn {
       reverse_proxy 127.0.0.1:8090
   }
   https://*.aie.example.vn {
       tls {
           on_demand
       }
       import /srv/aie/caddy/sites/*.caddy
       respond 404
   }
   ```

   Dùng wildcard thật: thay `tls { on_demand }` bằng `tls { dns <provider> {env.TOKEN} }` với Caddy build kèm module
   DNS (`xcaddy build --with github.com/caddy-dns/<provider>`).
5. **Chạy**: `docker compose -f docker-compose.saas.yml up -d` (control plane `network_mode: host`, lắng nghe
   `127.0.0.1:8090`; thư mục `tenants_dir` phải **cùng đường dẫn** trên host và trong container vì Docker của host
   phân giải bind mount của từng khách). Không dùng Docker cho control plane: `pip install -e .` rồi
   `python -m ai_employees saas --config saas.yaml` với người dùng có quyền Docker.
6. Thử không cần Docker: `backend: dry-run` (sinh tệp, ghi log lệnh) hoặc `backend: fake`; không khai `smtp` thì
   mã xác nhận và email chỉ ghi log.

## Vận hành

- **Trang công khai `/`**: giới thiệu, bảng giá từ `plans:`, form "Dùng thử miễn phí 14 ngày" (tên cửa hàng, tên,
  email, điện thoại, mật khẩu ≥ 10 ký tự, tên miền con gợi ý từ tên cửa hàng, gói, ngôn ngữ). Xác nhận email bằng
  mã 6 số (15 phút, 5 lần thử). Sau xác nhận: khách vào thẳng cổng khách hàng, thấy mật khẩu quản trị **một lần**,
  địa chỉ trang quản trị và website bán hàng, tiến độ khởi tạo (trang tự làm mới).
- **Cổng khách hàng `/portal`** (đăng nhập email + mật khẩu): gói, ngày dùng thử còn lại hoặc đã trả đến ngày nào,
  dung lượng đã dùng, hoá đơn. Thanh toán: hướng dẫn chuyển khoản (`bank:`) với nội dung `SAAS-<số hoá đơn>`; nút
  "Đã chuyển khoản" chuyển hoá đơn sang *Đã báo chuyển khoản* để người vận hành xác nhận. Đổi mật khẩu, cấp lại
  mật khẩu quản trị, yêu cầu/rút yêu cầu ngừng dịch vụ, tải bản sao lưu mới nhất (khi backend có tệp).
- **Bảng điều hành `/console`** (tài khoản trong `operators:`): bảng khách (trạng thái, gói, hết dùng thử, đã trả
  đến, hoá đơn gần nhất, xin ngừng), hoá đơn chờ xử lý (xác nhận đã thu / huỷ), tạo khách thủ công (khởi tạo ngay,
  không cần xác nhận email), trang từng khách: tạm dừng, mở lại, khởi tạo lại, cấp lại mật khẩu quản trị, đổi gói
  (ghi lại `limits` và khởi động lại), gia hạn dùng thử, xoá (gõ đúng slug; lưu trữ trước), nhật ký.
  "Chạy công việc hằng ngày ngay" để xử lý không chờ 3:00.
- **API JSON** cho tự động hoá: mọi `POST` nhận JSON khi có header `X-Requested-With: ai-employees` (thay cho
  token CSRF của form) và trả JSON; `GET /console`, `/console/tenants/<id>`, `/portal` trả JSON khi
  `Accept: application/json`. Đăng nhập trước bằng `POST /console/login` (cookie phiên).
- **Cổng thanh toán về sau**: `Billing.mark_paid(invoice, gateway, ref)` là điểm vào duy nhất để ghi nhận thanh
  toán (gia hạn `paid_until`, mở lại nếu đang tạm dừng, gửi email); IPN của cổng thanh toán gọi hàm này.
- **Sao lưu control plane**: `saas.sqlite` (hoặc PostgreSQL) + `tenants_dir` (chứa `.env` của khách) + `backups_dir`.
  Dữ liệu từng khách sao lưu bằng cơ chế của sản phẩm (xem `docs/OPERATIONS.md`).

### Bảo mật

Phiên HttpOnly, SameSite=Lax, Secure khi `public_url` là https; form có token CSRF (cookie + trường ẩn), API JSON
cần header `X-Requested-With`; giới hạn tần suất theo địa chỉ IP (đăng ký 5/giờ, đăng nhập và kiểm tra mã 10/15
phút); CSP `default-src 'self'` không script inline (trang do máy chủ dựng, một tệp CSS); không ghi mật khẩu, mã
hay nội dung email vào log; `.env` của khách quyền 0600; container khách chỉ mở cổng trên `127.0.0.1`.

## Giới hạn hiện tại

- Thanh toán thủ công (chuyển khoản + xác nhận của người vận hành); chưa có cổng thanh toán tự động, chưa xuất PDF.
- Chưa có giới hạn tài nguyên (CPU/RAM) cho container khách; `storage_mb` chỉ hiển thị, chưa chặn.
- Mọi khách dùng chung API key model của nền tảng (`tenant_env`); chưa đo và tính tiền theo lượng dùng model.
- Một máy chủ: control plane và các container khách cùng host. Nhiều máy chủ cần backend khác (giao thức
  `Backend` trong `provisioner.py`: `create/start/stop/destroy/status/reset_admin_password/usage`).
- Control plane chạy root (Docker socket). Bảng điều hành chưa có 2FA; hãy giới hạn IP truy cập `/console` ở Caddy.

## Lộ trình

1. Thanh toán tự động khi module cổng thanh toán (`payments.py`) hoàn tất: IPN → `Billing.mark_paid`; nút "Thanh
   toán ngay" trên hoá đơn dẫn tới trang cổng thanh toán thay cho hướng dẫn chuyển khoản.
2. Hạn mức tài nguyên và chặn theo `storage_mb`; báo cáo dùng model theo khách.
3. Backend đa máy chủ (điều phối qua SSH hoặc Nomad/Kubernetes) với cùng giao thức `Backend`.
4. Tự phục vụ đổi gói và nâng cấp trong cổng khách hàng, hoá đơn PDF, email nhắc trước hạn.
