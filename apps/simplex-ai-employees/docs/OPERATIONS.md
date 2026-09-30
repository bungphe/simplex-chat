# Vận hành văn phòng AI (sao lưu, giám sát, nâng cấp)

Tài liệu này dành cho người quản trị máy chủ. Mọi thứ ở đây nằm trong `src/ai_employees/ops.py`
và được bật sẵn khi chạy `python -m ai_employees run` (hoặc `docker compose up`); cấu hình bằng
biến môi trường trong `.env`, không cần sửa `employees.yaml`.

| Biến môi trường | Mặc định | Ý nghĩa |
|---|---|---|
| `AI_BACKUP_HOUR` | `3` | Giờ sao lưu hằng đêm, theo múi giờ của văn phòng (nhân viên đầu tiên trong `employees.yaml`) |
| `AI_BACKUP_DIR` | `<state_dir>/backups` (Docker: `/backups`) | Thư mục chứa các bản sao lưu |
| `AI_BACKUP_KEEP_DAYS` / `AI_BACKUP_KEEP_COUNT` | `14` / `60` | Giữ bản sao lưu trong bao nhiêu ngày và tối đa bao nhiêu tệp (bản mới nhất luôn được giữ) |
| `AI_BACKUP_COMMAND` | (không) | Lệnh chạy sau mỗi lần sao lưu để đưa tệp ra khỏi máy, `{file}` là đường dẫn tệp |
| `AI_BACKUP_ENABLED` | `1` | `0` tắt sao lưu tự động (và tắt cảnh báo "chưa có bản sao lưu") |
| `AI_METRICS_TOKEN` | (không) | Đặt để mở `GET /metrics`; chưa đặt thì đường dẫn này trả về 404 |
| `AI_HEALTHCHECK_URL` | `http://127.0.0.1:8080/healthz` | Địa chỉ mà `HEALTHCHECK` của Docker gọi; `off` để tắt |

## 1. Sao lưu

**Mỗi đêm** vào `AI_BACKUP_HOUR` (giờ văn phòng), tiến trình chính (shard 0) tạo tệp
`backup-YYYYmmdd-HHMMSS.tar.gz` (quyền `600`) trong `AI_BACKUP_DIR`. Nếu máy tắt đúng giờ đó,
bản sao lưu được làm ngay khi khởi động lại (mỗi ngày một bản; sao lưu lỗi thì thử lại sau một giờ).

Bên trong một bản sao lưu:

- `state/…` — toàn bộ `state_dir`: `office.sqlite` (cài đặt, tài khoản nhân sự, model thêm từ giao diện,
  token kênh), `inbox.db` (hộp thư, CRM, kho hàng, đơn hàng, bán hàng), `runlog.sqlite` (nhật ký),
  các tệp JSON/ảnh/tệp đính kèm. Cơ sở dữ liệu SQLite được chép bằng API sao lưu của SQLite
  (`sqlite3.Connection.backup`) nên **nhất quán dù văn phòng đang chạy**.
- `employees/<id>/<id>_chat.db`, `<id>_agent.db` — cơ sở dữ liệu SimpleX của từng nhân viên AI
  (tài khoản, danh bạ, khoá). Mất tệp này là mất địa chỉ SimpleX của nhân viên: khách phải kết nối lại.
- `postgres.dump` — khi văn phòng chạy PostgreSQL (`database_url`): `pg_dump --format=custom`
  (image Docker có sẵn `postgresql-client`; cài đặt thủ công thì `apt-get install postgresql-client`).
- `extra/…` — tệp cấu hình `employees.yaml` và các thư mục tài liệu (`knowledge_search.path`) nằm ngoài
  `state_dir`; `manifest.json` ghi đường dẫn gốc của chúng.

Bản sao lưu chứa API key, mật khẩu đã băm và dữ liệu khách hàng: **coi nó như mật khẩu**.

**Đưa bản sao lưu ra khỏi máy.** Sao lưu nằm cùng ổ đĩa với dữ liệu thì không cứu được khi mất máy.
Đặt `AI_BACKUP_COMMAND`; lệnh chạy không qua shell, `{file}` được thay bằng đường dẫn tệp vừa tạo:

```bash
# .env
AI_BACKUP_COMMAND=rclone copy {file} remote:aie-backups          # S3, Google Drive, Backblaze... (cấu hình rclone trước)
# hoặc
AI_BACKUP_COMMAND=scp {file} backup@backup-host:/srv/aie/          # cần khoá SSH không mật khẩu
```

Trong Docker, lệnh phải có trong image (rclone/scp không có sẵn): đơn giản hơn là để máy chủ sao lưu
`./backups` bằng cron/rclone của máy chủ, hoặc mount thư mục `./backups` vào một agent sao lưu khác.

**Sao lưu thủ công** (văn phòng có thể đang chạy):

```bash
python -m ai_employees backup data/employees.yaml
# Docker:
docker compose exec ai-employees python -m ai_employees backup /data/employees.yaml
ls -la backups/
```

Trạng thái lần sao lưu gần nhất (`last_ok`, `last_error`, `last_file`, `size`) nằm trong kho tài liệu
của văn phòng (khoá `ops_backup`), xem qua `GET /api/ops` (đăng nhập quản trị) hoặc `/metrics`.

## 2. Khôi phục

1. **Dừng văn phòng** (`docker compose stop ai-employees`, hoặc dừng dịch vụ systemd). Lệnh khôi phục
   từ chối chạy nếu `state_dir/office.pid` còn trỏ tới tiến trình đang sống trên cùng máy; trong Docker
   mỗi container một không gian pid nên kiểm tra này không thấy container khác — hãy dừng trước.
2. Khôi phục, `--yes` để xác nhận:

   ```bash
   python -m ai_employees restore backups/backup-20260929-030000.tar.gz --config data/employees.yaml --yes
   # Docker (container tạm, cùng volume):
   docker compose run --rm ai-employees restore /backups/backup-20260929-030000.tar.gz --config /data/employees.yaml --yes
   ```

   - `state_dir` hiện tại được đổi tên thành `<state_dir>.before-restore-<thời điểm>`; tệp SimpleX của
     nhân viên bị thay được giữ lại với cùng hậu tố (kể cả `-wal`/`-shm`). Khi chắc chắn mọi thứ ổn, xoá chúng.
   - Với PostgreSQL: `pg_restore --clean --if-exists` vào `database_url` trong cấu hình (cần
     `postgresql-client`). Mọi bảng hiện có bị thay bằng bản trong tệp sao lưu.
   - Thêm `--extras` nếu muốn khôi phục cả `employees.yaml` và thư mục tài liệu về đúng đường dẫn cũ
     (mặc định không, vì cấu hình thường được sửa tay).
   - `--force` bỏ qua kiểm tra `office.pid` (chỉ khi chắc chắn văn phòng đã dừng).
3. Khởi động lại (`docker compose start ai-employees`), kiểm tra `curl -s localhost:8080/healthz`,
   đăng nhập giao diện quản trị và nhắn thử một nhân viên qua SimpleX.

Nên **diễn tập khôi phục** mỗi quý trên một máy khác: chép tệp sao lưu và `employees.yaml`, chạy
`restore`, chạy văn phòng với `.env` của máy thử.

## 3. Kiểm tra sức khoẻ và số liệu

### `GET /healthz` (không cần đăng nhập)

Có trên giao diện quản trị (cổng 8080) và trên website bán hàng (cổng 8081). Trả về 200 khi cơ sở dữ liệu
trả lời một `SELECT 1`, 503 khi không:

```json
{"ok": true, "version": "0.1.0", "db": "ok", "employees": 4, "uptime_s": 86400}
```

Dùng cho `HEALTHCHECK` của Docker (đã cài trong `Dockerfile`/`docker-compose.yml`; `docker ps` hiện
`healthy`/`unhealthy`), cho reverse proxy, và cho dịch vụ theo dõi bên ngoài:

- **Uptime Kuma**: *Add New Monitor* → Monitor Type *HTTP(s)*, URL `https://admin.cua-hang.vn/healthz`
  (hoặc `https://shop.cua-hang.vn/healthz`), Heartbeat 60 giây, Accepted Status Codes `200-299`.
  Với "HTTP(s) - Keyword" điền keyword `"ok": true`. Thông báo qua Telegram/email khi đổi trạng thái.
- `curl -fsS http://127.0.0.1:8080/healthz || systemctl restart ai-employees` trong cron cũng được.

### `GET /metrics` (Prometheus)

Chỉ có khi đặt `AI_METRICS_TOKEN` (chuỗi ngẫu nhiên dài); gọi với header
`Authorization: Bearer <token>`. Chưa đặt biến: 404; sai token: 401.

```bash
curl -H "Authorization: Bearer $AI_METRICS_TOKEN" http://127.0.0.1:8080/metrics
```

| Số liệu | Ý nghĩa |
|---|---|
| `aie_up` | 1 khi cơ sở dữ liệu trả lời |
| `aie_uptime_seconds`, `aie_employees`, `aie_info{version}` | tiến trình |
| `aie_runs_15m{status="ok|error|busy|refused|…"}` | số lượt làm việc (trả lời, routine, hành động…) trong 15 phút qua theo kết quả |
| `aie_model_errors_15m` | số lượt lỗi gọi model AI trong 15 phút |
| `aie_inbox_unread` | tin khách chưa đọc trong hộp thư chung |
| `aie_orders_open` | đơn hàng chưa hoàn tất, chưa huỷ |
| `aie_channel_errors`, `aie_channel_error{channel}` | kênh chat (Zalo, Facebook…) đang lỗi |
| `aie_backup_ok`, `aie_backup_age_seconds`, `aie_backup_size_bytes`, `aie_backup_enabled` | sao lưu |
| `aie_disk_free_bytes`, `aie_disk_total_bytes` | ổ đĩa chứa `state_dir` |
| `aie_db_size_bytes` | tổng các tệp SQLite, hoặc `pg_database_size()` |

Cấu hình Prometheus:

```yaml
scrape_configs:
  - job_name: ai-employees
    scrape_interval: 60s
    authorization:
      credentials_file: /etc/prometheus/aie-metrics-token   # nội dung: AI_METRICS_TOKEN
    static_configs:
      - targets: ["127.0.0.1:8080"]
```

Luật cảnh báo gợi ý (Alertmanager):

```yaml
groups:
  - name: ai-employees
    rules:
      - alert: AIEmployeesDown
        expr: up{job="ai-employees"} == 0 or aie_up == 0
        for: 3m
      - alert: AIEmployeesBackupStale
        expr: aie_backup_enabled == 1 and (aie_backup_ok == 0 or aie_backup_age_seconds > 36*3600)
        for: 30m
      - alert: AIEmployeesDiskLow
        expr: aie_disk_free_bytes < 2*1024^3 or aie_disk_free_bytes / aie_disk_total_bytes < 0.10
      - alert: AIEmployeesModelErrors
        expr: aie_model_errors_15m > 5
      - alert: AIEmployeesChannelError
        expr: aie_channel_error == 1
        for: 15m
```

`GET /api/ops` (đăng nhập quản trị) trả về cùng các số liệu dạng JSON, kèm trạng thái sao lưu và
danh sách cảnh báo đang mở.

## 4. Cảnh báo tự động (watchdog)

Cứ 5 phút, tiến trình chính kiểm tra và **nhắn cho các quản trị viên** (danh sách admin của nhân viên
AI đầu tiên trong `employees.yaml` — đăng ký bằng `/admin <token>` trong SimpleX) khi:

| Điều kiện | Tin nhắn |
|---|---|
| Ổ đĩa chứa `state_dir` còn dưới 10% hoặc dưới 2 GB | `⚠️ Cảnh báo vận hành: Ổ đĩa sắp đầy: chỉ còn 1.5 GB trống (3%) tại /data/state` |
| Không có bản sao lưu nào thành công trong 36 giờ (khi sao lưu đang bật) | `… Không có bản sao lưu mới nào trong 40 giờ qua — lỗi gần nhất: …` |
| Một kênh chat báo lỗi ở lần lấy tin/gửi tin gần nhất | `… Kênh zalo-oa đang lỗi: token expired` |
| Hơn 5 lượt gọi model AI lỗi trong 15 phút (hết tiền API, sai key, model sập) | `… 8 lỗi gọi model AI trong 15 phút qua` |
| Không kết nối được PostgreSQL | `… Không kết nối được PostgreSQL: …` |

Mỗi điều kiện chỉ nhắn **một lần mỗi 6 giờ** khi còn kéo dài, và nhắn `✅ Đã ổn lại: <điều kiện>` khi hết.
Trạng thái đã nhắn lưu trong kho tài liệu (khoá `ops_alerts`) nên khởi động lại không nhắn trùng.
Việc cổng web (giao diện quản trị, website) có mở ra ngoài hay không thì tiến trình không tự thấy được:
dùng Uptime Kuma/Prometheus như mục 3.

## 5. Dung lượng

- **Ổ đĩa.** Dữ liệu tăng chủ yếu ở `inbox.db` (tin nhắn, ảnh đính kèm trong `state_dir/media` nếu có),
  `runlog.sqlite` (một dòng mỗi lượt làm việc, ~1 KB) và các bản sao lưu. Ước lượng: một cửa hàng
  1.000 hội thoại/ngày ≈ 1–2 GB/năm cho cơ sở dữ liệu; mỗi bản sao lưu nén còn khoảng 1/5. Với 14 ngày
  sao lưu, để trống ít nhất **5 lần** kích thước `state_dir`. Xem `aie_db_size_bytes` và
  `aie_disk_free_bytes` để dự báo.
- **Giới hạn gói SaaS**: `limits.storage_mb` trong cấu hình chặn khi vượt (xem README).
- **Bộ nhớ/CPU**: một tiến trình ~300–500 MB RAM khi rảnh; chi phí thật là tiền gọi model AI.
  Nhiều khách hơn: `cluster.shards` và PostgreSQL (README, "Mở rộng quy mô").
- Dọn dẹp: các thư mục `*.before-restore-*` sau khi khôi phục thành công; giảm `AI_BACKUP_KEEP_DAYS`
  nếu đã có bản sao lưu ngoài máy.

## 6. Nâng cấp phiên bản

```bash
cd apps/simplex-ai-employees
python -m ai_employees backup data/employees.yaml   # hoặc: docker compose exec ai-employees python -m ai_employees backup /data/employees.yaml
git pull                                           # (hoặc docker compose pull khi dùng image dựng sẵn)
docker compose build                               # dựng image mới
docker compose up -d                               # thay container; migration cơ sở dữ liệu chạy tự động khi khởi động
docker compose logs -f --tail=100 ai-employees     # đợi "admin UI: http://..." và địa chỉ SimpleX của các nhân viên
curl -s http://127.0.0.1:8080/healthz
```

- Schema cơ sở dữ liệu được tạo/bổ sung tự động khi khởi động (`CREATE TABLE IF NOT EXISTS`, thêm cột
  thiếu); không có bước migration thủ công. Không hạ cấp về phiên bản cũ sau khi phiên bản mới đã chạy;
  nếu cần quay lại, khôi phục bản sao lưu làm trước khi nâng cấp.
- Với nhiều tiến trình (`--profile scale`): nâng cấp `ai-employees` (shard 0) trước, rồi `ai-employees-2`.
- Cài đặt không Docker: `pip install -e ".[postgres]"` lại trong venv rồi khởi động lại dịch vụ.

## 7. Checklist trước khi lên production

- [ ] `.env`: `AI_ADMIN_PASSWORD` ≥ 12 ký tự ngẫu nhiên, `AI_ADMIN_TOKEN` ngẫu nhiên, key của model; `.env` quyền `600`, không commit.
- [ ] `python -m ai_employees check data/employees.yaml` không báo lỗi, model có key.
- [ ] Giao diện quản trị chỉ ở `127.0.0.1` (SSH tunnel hoặc reverse proxy HTTPS có xác thực); website bán hàng sau reverse proxy HTTPS, `storefront.public_url` đúng tên miền.
- [ ] `TZ` và `timezone` của nhân viên đúng múi giờ cửa hàng (giờ sao lưu, routine).
- [ ] Ít nhất một quản trị viên đã `/admin <token>` trong SimpleX với nhân viên đầu tiên (nơi nhận cảnh báo); nhắn thử bằng cách tạm đặt `AI_BACKUP_COMMAND=false` và chờ, hoặc kiểm tra `GET /api/ops`.
- [ ] Sao lưu: `./backups` thuộc uid 10001 (`sudo chown 10001:10001 backups`), `python -m ai_employees backup` chạy được, `AI_BACKUP_COMMAND` đưa tệp ra khỏi máy, đã **diễn tập khôi phục** một lần.
- [ ] Giám sát: Uptime Kuma/Prometheus gọi `/healthz` (và `/metrics` với `AI_METRICS_TOKEN`); `docker ps` hiện `healthy`.
- [ ] Ổ đĩa trống ≥ 5× `state_dir`, cảnh báo ổ đĩa đã hiểu (mục 4).
- [ ] `restart: unless-stopped` (compose) hoặc dịch vụ systemd `Restart=always`; máy chủ tự khởi động Docker sau khi reboot.
- [ ] Nhật ký: `docker compose logs` được xoay vòng (`/etc/docker/daemon.json`: `"log-driver": "json-file", "log-opts": {"max-size": "50m", "max-file": "5"}`).
- [ ] PostgreSQL (nếu dùng): `POSTGRES_PASSWORD` mạnh, volume `postgres` được sao lưu (bản `pg_dump` trong tệp sao lưu của văn phòng đã đủ), `pg_dump` cùng phiên bản lớn với máy chủ.
- [ ] Kênh chat: webhook (Zalo OA, Facebook, Telegram…) trỏ đúng URL HTTPS công khai; token trong `.env`.
- [ ] Đã đọc mục "Quyền riêng tư" trong README và thông báo cho khách về việc dùng trợ lý AI.
