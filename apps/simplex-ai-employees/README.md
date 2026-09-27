# SimpleX AI Employees

Biến tài khoản SimpleX Chat thành **nhân viên AI**: mỗi tài khoản là một agent có vai trò riêng
(system prompt), bộ **skill** (công cụ) riêng và được gán một **model AI** làm "trí thông minh".
Model có thể là Claude hoặc bất kỳ model nào có API chuẩn OpenAI: OpenAI, Gemini, DeepSeek, Groq,
OpenRouter, Mistral, hay model chạy tại chỗ (Ollama, vLLM, LM Studio). Nhiều nhân viên chạy trong
cùng một tiến trình, mỗi người làm một việc khác nhau, và có thể hỏi nhau (ví dụ bán hàng hỏi kế toán).

Khách hàng chỉ cần kết nối vào địa chỉ SimpleX của nhân viên rồi nhắn tin như với người thật.
Người quản lý cấu hình nhân viên **ngay trong chat** bằng lệnh `/ai …`.

```
Khách ──SimpleX (mã hoá đầu-cuối)──► Lan - Bán hàng ──ask_colleague──► Minh - Kế toán
                                     [model: claude]                   [model: gemini]
                                        │ knowledge_search, order_status, memory…
                                        └─handoff_to_human──► Quản lý (người thật)
                                     Hà - Viết nội dung [model: local / Ollama]
```

## Cài đặt

Cần Python 3.11+ và khoá API của các model bạn khai báo (model chạy tại chỗ thì không cần).

```bash
cd apps/simplex-ai-employees
python3 -m venv .venv && . .venv/bin/activate
pip install -e ../../packages/simplex-chat-python -e .
export ANTHROPIC_API_KEY=sk-ant-...        # cho model provider: anthropic
export GEMINI_API_KEY=...                  # tên biến do bạn đặt trong api_key_env
export AI_ADMIN_TOKEN=$(openssl rand -hex 16)
export AI_ADMIN_PASSWORD='<mật khẩu dài cho giao diện web>'
```

`libsimplex` (lõi SimpleX) được SDK tự tải về ở lần chạy đầu. Muốn dùng bản tự build từ mã nguồn
thì đặt `SIMPLEX_LIBS_DIR=<thư mục chứa libsimplex.so>`.

## Chạy

```bash
python -m ai_employees check examples/employees.yaml   # kiểm tra cấu hình, model và key
python -m ai_employees run examples/employees.yaml
```

Log in ra địa chỉ liên hệ của từng nhân viên. Dán địa chỉ vào ứng dụng SimpleX để bắt đầu chat.

## Triển khai bằng Docker

Trên máy chủ (Linux, có Docker), từ thư mục gốc của repo:

```bash
cd apps/simplex-ai-employees
mkdir -p data && cp examples/employees.yaml data/ && cp -r examples/knowledge examples/plugins data/
cp .env.example .env          # điền AI_ADMIN_PASSWORD, AI_ADMIN_TOKEN và API key của model
docker compose up -d --build  # thêm --profile local-ai để chạy kèm Ollama
docker compose logs -f        # xem địa chỉ SimpleX của từng nhân viên
```

- `data/` chứa cấu hình, tài liệu, cơ sở dữ liệu SimpleX và trạng thái; hãy sao lưu thư mục này.
- Giao diện quản trị chỉ mở ở `127.0.0.1:8080` của máy chủ. Truy cập từ xa bằng SSH tunnel
  (`ssh -L 8080:127.0.0.1:8080 <máy chủ>`) hoặc reverse proxy có HTTPS.
- Mặc định image tải `libsimplex` bản phát hành của SimpleX. Muốn dùng bản tự build từ repo này
  (`scripts/desktop/build-lib-linux.sh`), bỏ comment hai dòng `SIMPLEX_LIBS_DIR` trong `docker-compose.yml`
  và build với `--build-arg DOWNLOAD_LIBSIMPLEX=0`.
- Máy chủ đi Internet qua proxy kiểm tra TLS: thêm `--secret id=ca_cert,src=<ca-của-proxy.crt>` khi build.
  CA chỉ dùng lúc build, không nằm trong image.

**Chọn model.** Model chạy tại chỗ nhỏ (vd. `qwen2.5:7b` trên CPU) chạy được nhưng chậm (30–90 giây mỗi
câu) và gọi skill kém tin cậy: có lúc nói "đã tạo đơn" mà không gọi `create_order`. Hàng chờ duyệt giữ an
toàn trong trường hợp đó vì không có đơn nào đi ra ngoài. Dùng thật thì nên chọn model qua API (Claude, GPT,
Gemini) hoặc model lớn hơn chạy trên GPU; dùng quy tắc sửa sai (`/ai correct`) để chỉnh hành vi.

## Khai báo model AI

Khai báo mỗi model một lần trong mục `models:`, đặt cho nó một cái tên, rồi gán tên đó cho nhân viên.

```yaml
models:
  claude:
    provider: anthropic            # Claude, qua SDK chính thức của Anthropic
    model: claude-opus-5
    api_key_env: ANTHROPIC_API_KEY
  gemini:
    provider: openai               # mọi API chuẩn OpenAI Chat Completions
    base_url: https://generativelanguage.googleapis.com/v1beta/openai
    api_key_env: GEMINI_API_KEY
    model: gemini-2.5-flash
  local:
    provider: openai
    base_url: http://localhost:11434/v1   # Ollama chạy trên máy bạn
    model: qwen2.5:7b

employees:
  - id: accountant
    model: gemini                  # gán model cho nhân viên
```

| Trường | Ý nghĩa |
|---|---|
| `provider` | `anthropic` (Claude) hoặc `openai` (API chuẩn OpenAI của bất kỳ nhà cung cấp nào) |
| `model` | Tên model theo nhà cung cấp |
| `base_url` | Địa chỉ API, vd. `https://api.openai.com/v1`, `https://api.deepseek.com/v1`, `http://localhost:11434/v1` |
| `api_key_env` | Tên biến môi trường chứa API key (khuyên dùng); hoặc `api_key` ghi thẳng (không khuyên) |
| `headers` | Header bổ sung, vd. cho OpenRouter hay proxy nội bộ |
| `extra_body` | Trường bổ sung gửi kèm mỗi request, vd. `temperature`, `max_tokens` |
| `refusal_fallback` | Chỉ Claude: tự chuyển sang model dự phòng nếu yêu cầu bị từ chối (mặc định bật cho Opus 5/Fable) |
| `timeout` | Thời gian chờ mỗi request, giây (mặc định 120) |

Không khai báo gì thì nhân viên dùng `claude-opus-5`. Viết thẳng một mã model Claude (vd.
`model: claude-sonnet-5`) cũng được, không cần khai báo.

Lưu ý khi dùng model qua API chuẩn OpenAI:
- Model phải hỗ trợ *tool/function calling* thì mới dùng được skill.
- Skill `web_search` chạy trên máy chủ Anthropic nên chỉ có với model Claude; các model khác tự động không được cấp skill này.
- `effort` chỉ áp dụng cho Claude. Với model khác, đặt tham số qua `extra_body` (vd. `reasoning_effort`).

## Cấu hình nhân viên

Xem [examples/employees.yaml](examples/employees.yaml): ba nhân viên làm ba việc khác nhau (bán hàng, kế
toán, viết nội dung), mỗi người một model. Mỗi nhân viên gồm:

| Trường | Ý nghĩa |
|---|---|
| `id`, `display_name`, `short_descr` | Định danh, tên hiển thị, mô tả ngắn (đồng nghiệp AI cũng nhìn thấy) |
| `system_prompt` | Vai trò, nhiệm vụ, giới hạn của nhân viên |
| `model` | Tên model đã khai báo trong `models:` (hoặc mã model Claude) |
| `effort` | Chỉ Claude: mức suy nghĩ, mặc định `medium` |
| `skills`, `skill_config` | Các skill được bật và tuỳ chọn của từng skill |
| `welcome` | Lời chào gửi tự động khi có người kết nối |
| `history_messages` | Số tin nhắn nhớ được với mỗi người (mặc định 40) |
| `admin_token` / `admin_token_env` | Mã đăng nhập quản trị trong chat |
| `db` | Đường dẫn cơ sở dữ liệu SimpleX của tài khoản |

Đặt `servers.smp` nếu muốn dùng SMP server riêng thay cho server có sẵn của SimpleX.

## Skill có sẵn

| Skill | Chức năng |
|---|---|
| `knowledge_search` | Tìm trong tài liệu nội bộ (`.md`/`.txt` trong `skill_config.knowledge_search.path`) |
| `memory` (`remember`, `recall`, `search_conversation`, `learn`) | Trí nhớ dài hạn, xem mục *Trí nhớ* bên dưới. `notes` là nhóm cũ chỉ gồm `remember` + `recall` |
| `ask_colleague` | Hỏi nhân viên AI khác; giới hạn bằng `skill_config.ask_colleague.colleagues` |
| `handoff_to_human` | Chuyển yêu cầu cho quản trị viên (người thật) qua SimpleX |
| `current_time` | Ngày giờ hiện tại theo múi giờ |
| `web_search` | Tìm kiếm web (chạy trên máy chủ Anthropic, chỉ với model Claude) |
| `recent_conversations` | *Nội bộ*: đọc hội thoại gần đây của chính nhân viên, cho báo cáo |
| `office_report` | *Nội bộ*: tình hình cả văn phòng (hoạt động, lỗi, chờ duyệt, lịch) |
| hành động (vd. `create_order`) | Gọi webhook vào hệ thống của bạn, có hàng chờ duyệt (xem bên dưới) |

Skill *nội bộ* chỉ được dùng khi chạy lịch làm việc hoặc khi người chat là quản trị viên. Khách bình
thường không bao giờ khiến nhân viên đọc được hội thoại của người khác.

## Khách hàng nhiều nước (đa ngôn ngữ)

Nhân viên AI trả lời mỗi khách bằng ngôn ngữ của khách, dù tài liệu và vai trò viết bằng tiếng Việt.

- **Tự nhận biết ngôn ngữ** từ tin nhắn của khách (tiếng Việt có dấu hoặc không dấu, Anh, Trung, Nhật, Hàn,
  Thái, Indonesia, Pháp, Đức, Tây Ban Nha, Nga…) và nhớ cho từng khách. Tin mơ hồ như "ok", "hi" không làm
  đổi ngôn ngữ đã biết.
- **Nhân viên chọn nước hoặc ngôn ngữ của khách** trong Hộp thư (vd. *Nhật Bản · 日本語*); lựa chọn này được giữ,
  không bị tự nhận biết ghi đè. Chọn "Tự nhận biết" để quay lại.
- AI được dặn rõ: trả lời toàn bộ bằng ngôn ngữ của khách (kể cả khi vai trò ghi "luôn trả lời tiếng Việt"),
  tìm tài liệu bằng từ khoá tiếng Việt, **giữ nguyên giá và đơn vị tiền** (4.500.000đ = 4,500,000 VND), không
  tự quy đổi sang tiền nước khác.
- **Thông báo cố định** (hệ thống bận, từ chối, yêu cầu đã xác nhận / chưa được chấp nhận) có sẵn 13 ngôn ngữ.
  `confirm_message` của hành động có thể viết theo từng ngôn ngữ (`{vi: "...", en: "...", ja: "..."}`) hoặc
  một câu tiếng Việt: hệ thống tự dịch cho khách. Lý do từ chối quản lý gõ bằng tiếng Việt cũng được dịch.
- **Trong Hộp thư:** huy hiệu ngôn ngữ (JA, KO, EN…) cạnh tên khách; nút **Dịch** dưới tin của khách (bản dịch
  được lưu, dịch một lần); ô **"Viết tiếng Việt, tự dịch sang … khi gửi"**: nhân viên gõ tiếng Việt, khách nhận
  ngôn ngữ của họ, Hộp thư hiện cả bản gốc. Nút *Gợi ý trả lời* khi đó soạn nháp bằng tiếng Việt.
- Tóm tắt trí nhớ dài hạn luôn viết bằng tiếng Việt để nhân viên đọc được (`staff_language`, mặc định `vi`).

Hai chế độ trả lời khách nước ngoài (theo từng nhân viên):

```yaml
employees:
  - id: sales
    translate_replies: false     # mặc định: model trả lời thẳng bằng ngôn ngữ của khách
    # translate_replies: true    # dịch câu hỏi sang tiếng Việt → trả lời bằng tiếng Việt từ tài liệu → dịch
    #                            # câu trả lời sang ngôn ngữ khách (tốn thêm 2 lượt gọi model)
    # translation_model: gemini  # model riêng cho việc dịch (khai báo trong models:)
```

Khi dịch, giá tiền và mã sản phẩm được **khoá lại** trước khi đưa cho model rồi điền lại sau (model không thể
đổi "4.500.000đ" thành "4,500,000円" hay viết "MA-100" thành "マ-100"). Bản dịch không đúng ngôn ngữ (vd. dịch
sang tiếng Nhật mà ra tiếng Trung) được dịch lại một lần; vẫn sai thì **không gửi**: khách nhận thông báo bận
bằng ngôn ngữ của họ, tin nhắn nằm chờ nhân viên trong Hộp thư.

**Chọn model:** với khách nước ngoài nên dùng model đa ngôn ngữ tốt (Claude, GPT, Gemini…), hoặc giữ model
chạy tại chỗ để trả lời khách Việt và đặt `translation_model` là một model đa ngôn ngữ. Khi chạy thử với
Qwen 2.5 7B tại chỗ: khách tiếng Anh được trả lời đúng; với tiếng Nhật, tiếng Hàn model nhỏ này thường không
tra tài liệu mà tự đoán giá, và đôi khi lẫn sang tiếng Trung.

## Trí nhớ của nhân viên AI

Nhân viên AI nhớ theo ba tầng:

| Tầng | Nội dung | Ai ghi |
|---|---|---|
| Gần đây | `history_messages` tin gần nhất với từng khách (mặc định 40), gửi nguyên văn cho model | tự động |
| Dài hạn, từng khách | **Tóm tắt** mọi trao đổi cũ hơn (nhu cầu, sản phẩm, giá đã báo, đơn hàng, lời hứa, sở thích) và **ghi chú** (`remember`: số điện thoại, địa chỉ…). Cả hai được đưa vào mỗi câu trả lời, AI không cần tự gọi `recall` | AI tự tóm tắt khi tin cũ rời khỏi phần "gần đây" (gộp mỗi 6 tin để đỡ tốn lượt gọi model); nhân viên sửa được trong Hộp thư |
| Chung, mọi khách | **Ghi nhớ chung**: bài học dùng cho mọi cuộc trò chuyện (vd. "lắp ngoại thành phí 200.000đ") | AI đề xuất bằng skill `learn`; chỉ dùng sau khi quản trị **duyệt** (trang Nhân viên). Quản trị nói trong chat hoặc thêm trên web thì có hiệu lực ngay |

- `search_conversation` tìm trong **toàn bộ** lịch sử với khách đang chat (mọi tin trong Hộp thư, kể cả những tin
  đã rời khỏi phần "gần đây"), vd. mã đơn khách đưa từ tuần trước.
- Trí nhớ của khách nào chỉ dùng khi nói chuyện với khách đó. Khách không thể "dạy" AI điều sai cho người khác:
  mọi bài học AI rút ra từ khách đều phải được duyệt.
- Nếu model lỗi khi tóm tắt, tin cũ được giữ lại (tối đa 200) và tóm tắt lại ở lần sau, không bị mất.
- `/forget` trong chat hoặc nút *Xoá trí nhớ* xoá cả tin gần đây, tóm tắt và ghi chú của khách đó.
- Thứ tự ưu tiên khi có mâu thuẫn: quy tắc sửa sai > ghi nhớ chung > vai trò (system prompt).

### Tự viết skill

Tạo một module Python rồi khai báo trong `plugins:` (và `plugin_paths:` nếu cần):

```python
from ai_employees.skills import SkillError, skill

@skill("order_status", "Look up the delivery status of an order by its number.",
       {"order_id": {"type": "string", "description": "Order number, e.g. DH-1001"}})
async def order_status(ctx, order_id: str) -> str:
    ...  # gọi API / cơ sở dữ liệu của bạn
    raise SkillError("order not found")   # lỗi được trả lại cho AI để nó tự xử lý
```

`ctx` cho biết nhân viên nào đang gọi (`ctx.employee`), đang chat với ai (`ctx.contact_id`,
`ctx.contact_name`) và tuỳ chọn trong `skill_config` (`ctx.options`). Xem ví dụ đầy đủ ở
[examples/plugins/shop_skills.py](examples/plugins/shop_skills.py).

## Lịch làm việc (routine)

Nhân viên không chỉ trả lời tin nhắn mà còn **tự làm việc theo lịch** và gửi kết quả cho quản trị
viên qua SimpleX, ví dụ bản tóm tắt buổi sáng hay báo cáo cuối tuần.

```yaml
routines:
  - id: morning-brief
    days: mon-fri          # daily | weekdays | mon-fri | mon,wed,fri | sat-sun ...
    at: "08:00"            # giờ theo timezone của nhân viên
    window_minutes: 180    # máy tắt lúc 8h mà bật lại trước 11h thì vẫn chạy (mặc định 180)
    period: day            # day | week | month: mỗi kỳ chạy đúng một lần
    deliver: admins        # admins (gửi qua SimpleX) | none (chỉ lưu, xem trên giao diện)
    task: |
      Đọc hội thoại 24 giờ qua, tóm tắt khách nào cần gọi lại.
```

- Mỗi kỳ chạy **đúng một lần**. Bộ lập lịch có chạy trễ hay chạy lặp cũng không gây chạy trùng.
- `days: mon-fri` kết hợp `period: month` nghĩa là chạy vào ngày làm việc đầu tiên của tháng.
- Khi chạy lịch, nhân viên dùng được các skill nội bộ như `recent_conversations` (đọc hội thoại của chính
  mình) và `office_report` (tình hình cả văn phòng).
- Lệnh chat: `/ai routines` xem lịch, `/ai run <id>` chạy ngay, `/ai routine pause|resume <id>`.

## Hành động và hàng chờ duyệt

Hành động là việc **đi ra khỏi văn phòng và khó rút lại**: tạo đơn hàng, đặt lịch hẹn, báo cho nhà cung cấp…
Mỗi hành động được khai báo là một webhook, nên nối được với mọi hệ thống có địa chỉ HTTP (API riêng, n8n,
Zapier, Make, CRM) mà không cần viết code:

```yaml
actions:
  create_order:
    description: Create a sales order in the shop system.
    url: https://n8n.example.com/webhook/create-order
    headers: {Authorization: "Bearer ${ORDER_WEBHOOK_TOKEN}"}   # ${ENV} đọc lúc gửi, không lưu trong file
    fields:
      customer_name: Customer's full name
      items: Products and quantities
    confirm_message: Đơn hàng của bạn đã được xác nhận.
```

Mỗi hành động trở thành một skill cùng tên; bật nó trong `skills:` của nhân viên. Khi nhân viên dùng:

1. Yêu cầu vào **hàng chờ duyệt**; quản trị viên nhận tin nhắn "🔔 Cần duyệt #1…".
2. Quản trị viên duyệt (`/ai approve 1` hoặc nút trên giao diện web). Hệ thống gọi webhook, rồi báo lại cho khách.
3. Hoặc từ chối (`/ai reject 1 hết hàng`); khách nhận được lý do.

Khi đã tin tưởng, quản trị viên **mở kênh** cho hành động đó (`releases: [create_order]` trong file cấu
hình, `/ai release create_order` hoặc giao diện web). Từ đó nhân viên tự thực hiện, không cần duyệt, và mọi
lần thực hiện vẫn được ghi vào nhật ký. Chỉ quản trị viên mở được kênh; không gì khách hay AI viết ra có thể
làm việc này.

## Quy tắc sửa sai

Khi nhân viên làm chưa đúng ý, quản trị viên thêm một quy tắc có ghi ngày thay vì viết lại cả vai trò:
`/ai correct Không báo giá lõi lọc qua chat, mời khách gọi hotline.` Quy tắc được ưu tiên hơn vai trò,
có hiệu lực từ tin nhắn tiếp theo. Xem bằng `/ai corrections`, xoá bằng `/ai uncorrect <số>`.

## Nhật ký và nhân viên giám sát

Mọi việc (trả lời, hỏi đồng nghiệp, chạy lịch, hành động) được ghi vào `state_dir/runlog.jsonl`: trạng
thái, model, skill đã dùng, số token, thời gian. Nhật ký này là nguồn cho giao diện web và skill
`office_report`. Nhân viên mẫu **Quân - Chánh văn phòng** trong `examples/employees.yaml` dùng skill này
mỗi chiều để báo cho chủ: ai đang lỗi hay tạm dừng, yêu cầu nào chờ duyệt, lịch nào không chạy.

## Giao diện quản trị web

```yaml
admin_ui:
  host: 127.0.0.1        # mặc định chỉ truy cập từ chính máy chủ
  port: 8080
  password_env: AI_ADMIN_PASSWORD
```

Mở `http://127.0.0.1:8080`. Chủ đăng nhập với tên `admin` và mật khẩu trong `admin_ui`; tài khoản này luôn dùng
được, kể cả khi quên mật khẩu các tài khoản khác. Giao diện gồm các trang:

| Trang | Làm được gì |
|---|---|
| Tổng quan | Trạng thái từng nhân viên, số việc 24 giờ, yêu cầu chờ duyệt, lịch tiếp theo, địa chỉ SimpleX |
| Hộp thư | **Mọi kênh chat trong một màn hình**: SimpleX, Zalo OA, Zalo cá nhân, Messenger, Telegram, WhatsApp, email, webhook. Trả lời khách, AI gợi ý câu trả lời, tiếp quản / giao lại cho AI; nhãn, ghi chú nội bộ, câu trả lời mẫu, đóng/mở, giao cho người hoặc nhóm, AI tóm tắt hội thoại |
| Kho hàng | Sản phẩm, tồn theo kho, đơn nhập (container) tính giá vốn, lô FIFO, tự động định giá 5 giai đoạn, đơn bán, đặt trước, chuyển kho, kiểm kho, gợi ý đặt hàng lại, nhập/xuất CSV |
| Bán hàng | Bán tại quầy: nhiều đơn cùng lúc, tìm nhanh (F2), giảm % hoặc giá riêng, voucher, combo, gợi ý bộ theo ngân sách, thu tiền (cọc, tiền thối), in / gửi hoá đơn |
| Giao hàng | Lịch giao theo tháng, phụ phí lắp ráp / vác lầu, xếp chuyến, tối ưu lộ trình, tài xế, phiếu giao hàng |
| Marketing | Khuyến mại, voucher, combo, chi phí quảng cáo, tập khách remarketing, biến động giá tuần |
| Báo cáo | Lãi lỗ (P&L), hoa hồng, ca làm việc, chi phí, thông báo đầu ngày |
| Khách hàng | Danh bạ khách qua mọi kênh (tên, điện thoại, email, công ty, ghi chú), khách có thể trùng, gộp khách, công ty |
| SLA | Khách đang chờ, chờ quá hạn, thời gian trả lời của AI và từng nhân viên, việc của từng người/nhóm |
| Cài đặt hộp thư | Nhãn, câu trả lời mẫu, nhóm, quy tắc tự phân loại, mục tiêu thời gian trả lời |
| Kênh chat & SimpleX | Trạng thái từng kênh, lấy tin ngay, đăng nhập Zalo cá nhân bằng QR; địa chỉ SimpleX kèm mã QR, tạo link mời một lần, kết nối bằng link |
| Nhân viên | Sửa vai trò, gán model, mức suy nghĩ, bật/tắt skill, mở kênh hành động, quy tắc sửa sai, chạy/tạm dừng lịch, gỡ quản trị viên, khôi phục cấu hình gốc |
| Chờ duyệt | Duyệt hoặc từ chối yêu cầu của mọi nhân viên, xem lịch sử |
| Model AI | Xem model, **thêm model mới kèm API key**, thử kết nối, xoá |
| Hội thoại | Xem hội thoại và ghi chú về từng khách, xoá trí nhớ |
| Nhật ký | Mọi việc đã làm, lọc theo nhân viên và loại việc |
| Tài khoản | Tạo tài khoản cho từng nhân viên, vai trò, giới hạn kênh, khoá, đặt lại mật khẩu, xoá |

**Tài khoản và phân quyền.** Mỗi người có tài khoản riêng; tên hiển thị của tài khoản được ghi vào từng câu trả
lời gửi khách (không tự gõ được tên người khác).

| Vai trò | Được làm |
|---|---|
| Quản trị | Mọi trang |
| Nhân viên bán hàng | Chỉ Hộp thư: trả lời, gợi ý, tiếp quản, giao lại AI. Có thể giới hạn chỉ xem một số kênh (vd. chỉ Zalo OA) |

Mỗi người tự đổi mật khẩu bằng cách bấm vào tên mình ở góc trên. Khi quản trị khoá tài khoản, đổi vai trò, đổi
kênh hay đặt lại mật khẩu, người đó bị đăng xuất ngay. Tài khoản lưu trong `state_dir/users.json` (quyền `600`),
mật khẩu băm bằng scrypt.

Bảo mật của giao diện:
- Đăng nhập bằng tên và mật khẩu (tài khoản nhân viên cần ít nhất 10 ký tự; mật khẩu chủ nên dài ít nhất 12).
  Phiên đăng nhập nằm trong cookie `HttpOnly`, `SameSite=Strict`.
- Ảnh và tệp của khách được máy chủ tải hộ từ nền tảng: chỉ tải đúng địa chỉ đã lưu cùng tin nhắn, chỉ từ địa chỉ
  Internet công khai (không vào mạng nội bộ), tối đa 15 MB; tệp không phải ảnh luôn được tải về chứ không mở
  trong trang.
- Mọi thao tác thay đổi đều cần một header riêng mà trang web khác không gửi được (chống CSRF).
- API key chỉ nhập vào được, không bao giờ hiện lại. Key thêm từ giao diện được lưu trong `state_dir/office.json` với quyền `600`.
- Muốn truy cập từ xa, đặt giao diện sau reverse proxy có HTTPS. Đừng mở cổng trực tiếp ra Internet.

## Kênh chat và hộp thư chung

Ngoài SimpleX, nhân viên AI trả lời khách trên Zalo, Facebook Messenger, Telegram, WhatsApp, email và mọi
nền tảng khác qua webhook. Mọi hội thoại vào **Hộp thư** chung trên giao diện web (lưu ở `state_dir/inbox.db`).

```yaml
channels:
  - id: zalo-shop                  # Zalo Official Account: API chính thức, tự lấy tin định kỳ
    type: zalo_oa
    employee: sales                # nhân viên AI phụ trách kênh
    app_id: "1234567890"
    app_secret_env: ZALO_APP_SECRET
    access_token_env: ZALO_ACCESS_TOKEN
    refresh_token_env: ZALO_REFRESH_TOKEN
    poll_seconds: 30               # lấy tin mỗi 30 giây (mặc định)
    debounce_seconds: 5            # chờ khách nhắn xong rồi mới trả lời một lần
  - id: zalo-canhan                # tài khoản Zalo cá nhân, qua zalo-gateway/ (đăng nhập bằng QR)
    type: zalo_personal
    employee: sales
    gateway_url: http://zalo-gateway:3000
    api_key_env: ZALO_GATEWAY_KEY
    secret_env: ZALO_GATEWAY_HOOK_SECRET
  - id: fanpage                    # Facebook Page / Messenger
    type: facebook
    employee: sales
    page_id: "100000000000000"
    access_token_env: FB_PAGE_TOKEN
  - id: website                    # nền tảng khác (chat trên web, cầu nối n8n…)
    type: webhook
    employee: sales
    secret_env: WEBCHAT_SECRET
    reply_url: https://example.com/chat/reply
    auto_reply: false              # chỉ gom tin về hộp thư, người trả lời
```

Cách hoạt động:
- **AI trả lời trước.** Khách nhắn, nhân viên AI được gán trả lời (dùng skill, kho kiến thức, trí nhớ
  riêng từng khách như trên SimpleX). Lần đầu kết nối một kênh, tin cũ chỉ được nhập vào làm ngữ cảnh, không trả lời lại.
- **Không bỏ sót khách khi hệ thống tắt hay khởi động lại.** Tin đến trong lúc tắt, tin đang chờ AI trả lời
  lúc khởi động lại, hay tin chưa trả lời được vì model lỗi đều được AI trả lời khi hệ thống chạy lại, nếu tin
  không cũ quá `catch_up_hours` (mặc định 12 giờ, khai báo ở đầu `employees.yaml`). Tin cũ hơn nằm chờ nhân viên.
- **Ảnh, tệp, sticker, link** của khách hiện ngay trong Hộp thư (ảnh xem trực tiếp, tệp tải về). AI biết khách
  đã gửi gì nhưng không xem được nội dung, nên sẽ hỏi khách mô tả hoặc chuyển cho người thật. Webhook gửi tệp
  bằng `"attachments": [{"kind": "image", "url": "https://…", "name": "…"}]`.
- **Người tiếp quản bất cứ lúc nào.** Nhân viên trả lời trong Hộp thư (hoặc trả lời thẳng trên Zalo/Facebook)
  thì hội thoại chuyển sang chế độ "người trả lời" và AI im lặng. Bấm **Giao lại cho AI** để AI tiếp tục; AI
  nhớ cả những gì nhân viên đã nói.
- **Gợi ý trả lời:** AI soạn sẵn câu trả lời vào ô nhập để nhân viên sửa rồi gửi. Chế độ này không gửi gì,
  không tạo đơn, không ghi vào trí nhớ.
- Nếu không gọi được model AI, hệ thống **không** gửi câu xin lỗi cho khách trên các kênh này; tin nhắn nằm
  chờ (chưa đọc) cho nhân viên.
- Hành động cần duyệt (tạo đơn…) và `handoff_to_human` hoạt động như trên SimpleX; tin xác nhận gửi về
  đúng kênh của khách.

**Nhận tin tức thì bằng webhook chính thức** (thay vì hỏi 30 giây/lần). Cần đặt giao diện sau HTTPS công khai,
chỉ mở đường `/hooks/`:

- *Messenger*: thêm `app_secret_env` (App Secret của ứng dụng Meta) và `verify_token` vào kênh `facebook`; trong
  Meta for Developers đặt Callback URL `https://<máy chủ>/hooks/<id kênh>`, Verify Token như trên, đăng ký sự
  kiện `messages` và `message_echoes`. Mỗi yêu cầu được kiểm chữ ký `X-Hub-Signature-256`.
- *Zalo OA*: thêm `webhook_secret_env` (OA Secret Key trong trang quản lý ứng dụng Zalo) vào kênh `zalo_oa`; đặt
  Webhook URL `https://<máy chủ>/hooks/<id kênh>` và bật các sự kiện `user_send_*` (và `oa_send_*` để thấy nhân viên
  trả lời trên Zalo). Mỗi yêu cầu được kiểm chữ ký `X-ZEvent-Signature` (sha256 của app_id + nội dung + timestamp +
  OA Secret Key) theo tài liệu Zalo; hãy thử với OA thật khi cài đặt.
- Có webhook thì đặt `poll_seconds: 0` để tắt việc hỏi định kỳ (hoặc giữ để bù tin lỡ). Sự kiện chỉ có mã khách,
  nên tên khách được hỏi thêm từ API của nền tảng. Tin gửi lại hai lần chỉ được lưu và trả lời một lần.

**Zalo OA** là cách được khuyến nghị cho bán hàng: API chính thức, token làm mới tự động (refresh token
mới được lưu trong `state_dir/channel_secrets.json`, quyền `600`).

**Zalo cá nhân** dùng gateway trong `zalo-gateway/` (chỉnh sửa từ `zalo_personal` của m.agent, thư viện
zca-js). zca-js **không phải API chính thức**: Zalo có thể hạn chế tài khoản dùng cách này, nên dùng một
tài khoản riêng cho bán hàng. Chạy gateway: đặt `ZALO_GATEWAY_KEY` và `ZALO_GATEWAY_HOOK_SECRET` (chuỗi ngẫu
nhiên dài) trong `.env`, `docker compose --profile zalo up -d`, rồi vào **Kênh chat & SimpleX → Đăng nhập
Zalo (QR)** và quét mã bằng app Zalo. Id của kênh chính là id tài khoản trên gateway. Phiên đăng nhập nằm
trong volume `zalo-sessions`; coi như mật khẩu.

**Telegram, WhatsApp, email** (dựng lại từ các adapter của m.agent; mọi yêu cầu vào đều được xác thực):

```yaml
  - id: telegram
    type: telegram
    employee: sales
    bot_token_env: TELEGRAM_BOT_TOKEN      # từ @BotFather
    secret_env: TELEGRAM_WEBHOOK_SECRET    # chuỗi ngẫu nhiên; Telegram gửi lại trong mỗi tin
    public_url: https://shop.example.com   # địa chỉ HTTPS công khai của máy chủ này
  - id: whatsapp
    type: whatsapp                         # qua máy chủ WAHA (github.com/devlikeapro/waha)
    employee: sales
    waha_url: http://waha:3000
    session: default
    api_key_env: WAHA_API_KEY
    hmac_key_env: WAHA_WEBHOOK_HMAC
  - id: email
    type: email
    employee: sales
    secret_env: EMAIL_INBOUND_KEY
    smtp_host: smtp.example.com            # smtp_port: 587, smtp_tls: starttls | ssl | none
    smtp_user: support@example.com
    smtp_password_env: SMTP_PASSWORD
    smtp_from: "Shop Minh An <support@example.com>"
```

- *Telegram*: bấm **Đăng ký webhook** trên trang Kênh chat (gọi `setWebhook` với `public_url/hooks/<id kênh>` và
  `secret_token`). Chỉ trả lời chat riêng, không trả lời trong nhóm. Ảnh/tệp của khách được máy chủ tải qua Bot API;
  token bot không bao giờ hiện trong giao diện hay nhật ký.
- *WhatsApp*: trong WAHA đặt webhook `https://<máy chủ>/hooks/whatsapp`, sự kiện `message` (và `message.any` để thấy
  nhân viên trả lời từ điện thoại), `hmac.key` = `WAHA_WEBHOOK_HMAC` (kiểm chữ ký `X-Webhook-Hmac`, SHA-512). Bỏ qua
  nhóm và status. Tệp của khách tải từ WAHA bằng API key. WAHA cũng dùng API không chính thức của WhatsApp: nên dùng
  số riêng cho bán hàng, hoặc WhatsApp Business API qua cầu nối `webhook`.
- *Email*: nhận thư bằng SendGrid Inbound Parse (hoặc dịch vụ gửi form tương tự) tới
  `https://<máy chủ>/hooks/email?key=<EMAIL_INBOUND_KEY>` (hoặc Basic Auth với mật khẩu là key). Trả lời qua SMTP, đúng
  luồng thư (`Re:`, `In-Reply-To`, `References`), phần trích dẫn thư cũ được cắt bỏ. Thư tự động, thư báo lỗi,
  danh sách thư (`Auto-Submitted`, `Precedence`, `List-Id`, `noreply@`) không được trả lời, nên không có vòng lặp
  thư. Tệp đính kèm chỉ giữ tên (xem trong hộp thư gốc).

**Webhook** cho mọi nền tảng khác. Cầu nối của bạn gửi tin của khách:

```bash
curl -X POST http://127.0.0.1:8080/hooks/website -H "X-Hook-Secret: $WEBCHAT_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"conversation_id": "visitor-42", "customer_name": "Linh", "text": "Shop có ship COD không?", "message_id": "m-1"}'
```

Câu trả lời được POST tới `reply_url` dạng `{"conversation_id", "text"}`; hoặc cầu nối tự đọc
`GET /hooks/website/visitor-42?after=<id tin cuối>` (cùng header). `message_id` giúp bỏ tin gửi trùng.

Webhook `/hooks/...` dùng chung cổng với giao diện quản trị. Nếu nền tảng ở ngoài máy chủ cần gọi vào, chỉ
mở đường dẫn `/hooks/` qua reverse proxy có HTTPS, không mở `/api/` và trang quản trị.

### Làm việc nhóm trong hộp thư

- **Trạng thái:** hội thoại *đang mở* hoặc *đã đóng*. Đóng khi xong việc; khách nhắn lại thì tự mở lại.
- **Giao việc:** giao cho một người, một nhóm, hoặc cả hai. Bộ lọc *Của tôi* gồm hội thoại giao cho mình và
  hội thoại giao cho nhóm của mình mà chưa có ai nhận; *Chưa giao cho ai*; *Khách đang chờ*; lọc theo nhãn.
- **Ghi chú nội bộ:** tick *Ghi chú nội bộ* khi soạn: tin chỉ nhân viên thấy (nền vàng), không gửi cho khách,
  không đưa vào trí nhớ hay công cụ tìm kiếm của AI, không có trong `GET /hooks/<kênh>/...` của cầu nối.
- **Câu trả lời mẫu:** chọn trong ô *Câu trả lời mẫu…*; `{name}` được thay bằng tên khách. Sửa trước khi gửi.
- **Tóm tắt (AI):** AI đọc cả hội thoại (kể cả ghi chú) và viết bản tóm tắt cho người tiếp nhận: khách cần gì, đã
  hứa gì, vấn đề, việc tiếp theo. Không lưu, trừ khi bấm *Lưu thành ghi chú*.
- **Quy tắc tự phân loại** (Cài đặt hộp thư): với mỗi tin mới của khách, nếu đúng kênh và có từ khoá (không phân
  biệt dấu, hoa thường: "khieu nai" khớp "Khiếu nại") thì gắn nhãn, giao nhóm/người (chỉ khi chưa có ai phụ trách),
  hoặc chuyển cho người trả lời (AI dừng ở hội thoại đó). Ví dụ: "khiếu nại, hoàn tiền, lừa đảo" → nhãn *Khiếu
  nại*, nhóm CSKH, AI dừng.
- **SLA:** mỗi lần khách chờ được tính từ tin đầu tiên chưa được trả lời tới câu trả lời của AI hoặc nhân viên (tin
  hệ thống và ghi chú không tính). Trang SLA cho thấy số khách đang chờ, chờ quá mục tiêu, thời gian trả lời trung
  bình, lâu nhất, tỉ lệ đúng hạn theo AI và từng nhân viên, và danh sách khách chờ lâu nhất. Chỉ quản trị viên xem.

### Khách hàng qua nhiều kênh (CRM)

Mỗi hội thoại (một khách trên một kênh) thuộc về một **khách hàng**. Khách mới nhắn thì có hồ sơ riêng; số điện
thoại, email khách tự viết trong tin (và số WhatsApp, địa chỉ email của kênh) được điền vào hồ sơ nếu còn trống,
không ghi đè thông tin nhân viên đã nhập.

- **Có thể trùng:** các hồ sơ cùng số điện thoại (0901 234 567 = +84 901 234 567) hoặc cùng email được liệt kê ở
  trang *Khách hàng* và trong khung *Khách hàng* của hội thoại. Không có gì bị gộp tự động.
- **Gộp:** quản trị viên bấm *Gộp vào đây*: mọi hội thoại về một hồ sơ, thông tin còn trống được lấy từ hồ sơ kia.
  Từ đó **AI nhớ khách qua các kênh**: khi trả lời trên Zalo, AI biết khách đã hỏi gì trên web (tóm tắt và vài
  tin gần nhất), cùng tên, số điện thoại, công ty. Gộp nhầm thì *Tách hội thoại này ra*.
- **Công ty:** nhóm nhiều người mua của một khách doanh nghiệp. Khai báo tên miền email (vd. `abc.com.vn`) thì khách
  viết từ `...@abc.com.vn` tự được gắn vào công ty.
- Nhân viên bán hàng xem và sửa thông tin khách trong Hộp thư, chỉ thấy các kênh mình được xem; trang *Khách hàng*,
  gộp và tách dành cho quản trị viên.

Nhân viên bán hàng dùng được mọi tính năng trên trong các kênh mình được xem; nhãn, câu mẫu, nhóm và quy tắc do
quản trị viên khai báo.

## Kho hàng, nhập hàng và tự động định giá

Dựng lại từ dự án sale-management (module sản phẩm và định giá 5 giai đoạn, module kho và đặt hàng nhà cung cấp),
trên cơ sở dữ liệu của văn phòng (SQLite hoặc PostgreSQL). Trang **Kho hàng** gồm:

- **Kho & nhà cung cấp:** nhiều kho tổng và cửa hàng (mỗi cửa hàng là một kho con); nhà cung cấp với thời gian giao
  hàng (ngày), dùng để tính điểm đặt hàng lại.
- **Sản phẩm:** SKU, nhóm biến thể, thuộc tính, CBM; tồn theo kho (có sẵn, đang giữ, có thể bán), hàng sắp về và
  ngày về, mức tồn (hết hàng / cần đặt / thấp / vừa / tốt). Nhập và xuất CSV (mở bằng Excel). Tồn đầu kỳ cho hàng
  đã có trước khi dùng hệ thống; kiểm kho có ghi lý do.
- **Nhập hàng (đơn nhập / container):** giá vốn cập bến = giá mua × tỷ giá + cước và thuế cả container chia theo
  thể tích (CBM), hoặc theo giá trị nếu không có CBM. Nhập **% lãi mục tiêu** thì có giá giai đoạn 1
  (= giá vốn / (1 − lãi)), hoặc nhập giá thì ra % lãi; giá giai đoạn 2-5 tự sinh theo mức giảm (mặc định 10%, 25%,
  35%, 50%), làm tròn tới 1.000 đ, sửa được từng giá; giá dưới giá vốn được cảnh báo. Nhận hàng theo số thực nhận
  (ghi số hỏng), nhận nhiều đợt được.
- **Lô FIFO:** mỗi đợt nhận là một lô với giá vốn và 5 giá riêng. Sản phẩm bán theo giá của **lô đang bán**; lô mới
  xếp hàng chờ đến khi lô cũ bán hết, rồi lên bán ở **giá giai đoạn 1** (hàng mới về).
- **Tự động định giá mỗi đêm** (giờ cài đặt được, mặc định 0 giờ, để giá không đổi giữa giờ bán) và nút **Cập nhật
  giá ngay**: lô chuyển sang giai đoạn kế tiếp khi hàng còn lại (hàng chưa bán và chưa giữ / số nhận trừ phần khách
  đặt trước) xuống dưới ngưỡng **và** đã giữ giá đủ số ngày tối thiểu, **hoặc** đã giữ giá quá số ngày tối đa (hàng
  bán chậm). Mặc định: 80% / 60% / 40% / 20%, tối thiểu 7 ngày, tối đa 14 / 30 / 45 / 60 ngày; từng sản phẩm có quy
  tắc riêng hoặc tắt tự định giá; quản lý đặt giai đoạn bằng tay. Mọi lần đổi giá ghi lại lý do.
- **Khách VIP** (đánh dấu trong hồ sơ khách): giá VIP riêng của sản phẩm/lô nếu có, không thì giá giai đoạn kế tiếp.
- **Đơn bán:** xác nhận là giữ hàng (theo lô FIFO để tính giá vốn); *Đã giao* thì trừ kho và ghi lãi; huỷ thì trả
  hàng về. **Đặt trước** hàng đang về: giữ chỗ trên đơn nhập; khi hàng về, đơn đặt trước được phục vụ trước.
- **Chuyển kho:** tạo phiếu, xuất kho, cửa hàng kiểm đếm và ký nhận; hàng thiếu ghi vào sổ kho.
- **Đặt hàng lại:** tốc độ bán 30 ngày × thời gian giao của nhà cung cấp + tồn an toàn (mặc định 7 ngày bán) = điểm
  đặt hàng lại; khi tồn có thể bán + hàng đang về ≤ điểm này, gợi ý số cần đặt (đủ bán thêm 30 ngày sau khi hàng về).

**Nhân viên AI bán từ kho thật.** Thêm skill `products` cho nhân viên bán hàng: AI tra giá hôm nay cho đúng khách này
(VIP hay không), số còn, hàng sắp về, và không bao giờ thấy giá vốn. Thêm hành động `kind: stock_order` (xem
`examples/employees.yaml`): khi khách chốt, AI tạo yêu cầu "SKU x số lượng"; quản lý duyệt thì đơn được tạo và hàng
được giữ, gắn với hội thoại và hồ sơ khách. Trong Hộp thư, nhân viên gõ tên sản phẩm ở ô *Tìm sản phẩm* để chèn
giá và tồn vào câu trả lời.

## Bán hàng, giao hàng, marketing và báo cáo

Phần còn lại của sale-management (module 1, 3, 4, 5), cùng cơ sở dữ liệu với kho hàng.

**Vai trò nhân viên** (trang Tài khoản): *Quản lý cửa hàng* (mọi trang vận hành, huỷ và hoàn tác đơn, không đụng
tài khoản, model, cấu hình AI), *Nhân viên bán hàng* (hộp thư và bán hàng), *Thu ngân* (bán hàng), *Thủ kho* (kho
hàng), *Điều phối giao hàng*, *Marketing* (marketing, báo cáo, xem sản phẩm và khách).

**Bán tại quầy (POS).** Mở nhiều đơn cùng lúc (giữ trong trình duyệt đến khi xong), tìm sản phẩm thấy ngay giá hôm
nay cho khách đó (VIP, khuyến mại), tồn từng kho, hàng sắp về và ngày về. Mỗi dòng giảm theo % hoặc nhập giá riêng
(ra % giảm tương ứng); voucher; combo. Đơn *lấy ngay* giữ hàng trong kho; *đặt trước* giữ chỗ trên container sắp về.
Thu tiền nhiều lần (cọc rồi trả nốt) bằng tiền mặt (gợi ý tiền khách đưa, tính tiền thối), thẻ, chuyển khoản, ví,
COD; mỗi lần thu có mã chống ghi trùng. *Đã giao (xuất kho)* trừ kho và ghi lãi thực tế theo lô FIFO. In hoá đơn (tên
cửa hàng, MST, VAT đã gồm, tài khoản ngân hàng khi còn nợ) hoặc gửi hoá đơn qua kênh chat của khách. Nhân viên chỉ
thấy đơn mình tạo trong ngày; quản lý thấy tất cả, huỷ đơn chưa giao (trả hàng giữ, hoàn tiền) và nhận trả hàng
trong số giờ cho phép (quản trị viên: bất cứ lúc nào).

**Tích điểm và VIP tự động** (Kho hàng → Cửa hàng & tích điểm): mỗi đơn đã giao của khách có hồ sơ được cộng điểm
(mặc định 1 điểm / 100.000 đ); đủ số điểm cài đặt thì khách tự lên VIP, nhận lời chúc mừng kèm số thẻ VIP trên kênh
chat họ dùng, quản lý được báo, và từ đơn sau được giá VIP. Trả hàng thì trừ lại điểm; quản lý cộng/trừ điểm tay được.

**Khuyến mại.** Giảm % (đi theo giá tự động theo giai đoạn: giá gốc đổi thì giá sale đổi theo) hoặc số tiền, cho sản
phẩm, danh mục hoặc mọi sản phẩm, có ngày bắt đầu/kết thúc và nhãn (Hot Deal, Xả kho…). Khách luôn được mức tốt nhất
trong giá giai đoạn, khuyến mại và giá VIP, không cộng dồn. **Voucher** có đơn tối thiểu, giảm tối đa, số lượt.
**Combo** bán nhiều sản phẩm một giá (phần tiết kiệm trừ vào đơn), hiện số bộ còn bán được.

**Gợi ý bộ sản phẩm** (phòng khách, phòng ngủ, phòng ăn…; sửa trong Marketing): các bộ vừa ngân sách, mọi món có sẵn
hoặc về trong 30 ngày, dùng ngân sách tốt nhất trước. Có ở màn hình bán hàng và cho nhân viên AI (skill `suggest_set`).

**Giao hàng.** Đặt lịch cho đơn đã xác nhận: ngày, khung giờ hoặc giờ cố định, địa chỉ, lắp ráp, vác lầu (phụ phí cộng
vào đơn). Lịch tháng hiện số đơn, số kiện, đơn đặc biệt mỗi ngày. Xếp chuyến theo xe (đơn vị vận chuyển, tài xế), *Tối
ưu lộ trình* (đường ngắn nhất rồi quay về kho; có `GOOGLE_MAPS_API_KEY` thì tìm toạ độ và thời gian chạy xe qua Google
Maps, không thì theo toạ độ nhập tay), kéo thứ tự bằng tay, giờ đến dự kiến từng điểm. *Xuất phát*: khách được nhắn
"đơn đang được giao" kèm giờ và tài xế. *Đã giao*: đơn hoàn tất, trừ kho, ghi lãi; *Quay về*: hàng vẫn giữ, đặt lịch
lại. Phiếu giao hàng CSV cho tài xế (khách, hàng và số kiện theo kho, ô ký nhận, ghi chú); chi phí từng đơn vị vận
chuyển theo kỳ.

**Báo cáo.** Lãi lỗ theo kỳ: doanh thu và giá vốn FIFO của hàng đã giao, quảng cáo (chia theo số ngày của chiến dịch
trong kỳ), chi phí (thuê mặt bằng, điện nước, lương…, bảo hiểm trên lương), hoa hồng đã chốt, chi phí giao hàng.
**Hoa hồng** = (doanh số đơn đã giao trong kỳ − số giờ làm × định mức / giờ) × % hoa hồng; phần đóng góp của chủ
(bảo hiểm) là chi phí thêm, không trừ vào hoa hồng. Đơn AI chốt được tính cho nhân viên phụ trách hội thoại, nếu có.
**Tập khách** (mua nhiều nhất, VIP, theo kênh) xuất CSV; **biến động giá tuần** (giá đầu tuần, giá nay, đã bán, lãi,
tồn). **Thông báo đầu ngày**: quản lý đăng, mọi người thấy cửa sổ thông báo khi đăng nhập đến khi xác nhận.

**Sàn TMĐT và website** (Kho hàng → Sàn TMĐT, quản trị viên): giá hoặc tồn đổi là sản phẩm được đẩy lên trong vòng 1
phút. *Amazon*: Selling Partner API (Listings Items: giá quy đổi sang tiền tệ của sàn và số lượng), lấy đơn Amazon về
(giữ hàng; Amazon báo đã gửi thì hoàn tất, huỷ thì huỷ), chỉ đọc SKU và số lượng. Khoá chỉ nằm trong biến môi trường
(`AMAZON_LWA_CLIENT_ID`, `AMAZON_LWA_CLIENT_SECRET`, `AMAZON_REFRESH_TOKEN` hoặc tên bạn chọn). *Webhook*: website của
bạn nhận giá và tồn, ký HMAC-SHA256 (`X-Signature`). Mỗi sản phẩm dùng SKU khác trên sàn, hoặc `-` để không bán ở đó.
Website cũng có thể đọc `GET /hooks/catalog?key=…` (bật bằng biến `CATALOG_KEY`; chỉ giá bán và tồn, không có giá vốn).

Chưa có: tài khoản đăng nhập website cho khách VIP (hệ thống không có website bán hàng), gửi hoá đơn qua email riêng
(gửi qua kênh chat, gồm kênh email nếu khách dùng), bán kính 30 km quanh showroom khi lọc khách.

## Mở rộng quy mô và đo tải

**Lưu trữ.** Mọi dữ liệu nằm trong cơ sở dữ liệu: mặc định là các file SQLite trong `state_dir` (quyền `600`),
hoặc một PostgreSQL dùng chung cho nhiều tiến trình:

```yaml
database_url_env: DATABASE_URL   # vd. postgresql://aie:***@db:5432/aie (pip install "simplex-ai-employees[postgres]")
cluster:
  shards: 4                      # số tiến trình; mỗi tiến trình chạy với AIE_SHARD=0..3
```

- Mỗi hội thoại thuộc về một tiến trình (theo số hội thoại), chỉ tiến trình đó trả lời, nên khách không bao giờ
  nhận hai câu trả lời. Tin đến tiến trình nào cũng được (sau bộ cân bằng tải); tiến trình đó lưu tin rồi báo
  cho tiến trình phụ trách qua PostgreSQL `NOTIFY`.
- Tiến trình số 0 chạy các tài khoản SimpleX, lịch làm việc và việc lấy tin Zalo OA/Facebook. Tin SimpleX gửi từ
  tiến trình khác (nhân viên trả lời, xác nhận đơn, báo quản lý) đi qua bảng `simplex_outbox`.
- Tài khoản nhân viên, phiên đăng nhập, model thêm từ giao diện và token Zalo nằm trong cơ sở dữ liệu nên mọi
  tiến trình dùng chung. Dữ liệu cũ (các file `*.json`, `runlog.jsonl`) được chuyển vào một lần khi khởi động.
- Docker: `docker compose --profile scale up -d` chạy thêm PostgreSQL và tiến trình thứ hai (xem `docker-compose.yml`).

**Đo tải** bằng `loadtest/loadtest.py`: model giả (trễ cố định 1 giây), kênh webhook, hàng nghìn hội thoại:

```bash
python loadtest/loadtest.py --rates 50,100,200 --conversations 10000
python loadtest/loadtest.py --processes 4 --database-url postgresql://... --rates 200,300,400
```

Kết quả đo trên máy thử 4 nhân (chung máy với PostgreSQL, bộ tạo tải và model giả):

| Cấu hình | Chịu được (trả lời ~1,2–1,3 giây) |
|---|---|
| Trước khi chuyển sang cơ sở dữ liệu (file JSON) | ~25 tin/giây (CPU đầy ở 50/giây) |
| 1 tiến trình, SQLite | ~150 tin/giây |
| 1 tiến trình, PostgreSQL | ~100 tin/giây |
| 4 tiến trình, PostgreSQL | ~300 tin/giây |

Giới hạn tiếp theo: mỗi tin cần khoảng 23 lượt truy vấn cơ sở dữ liệu, gọi đồng bộ; chuyển sang truy vấn bất
đồng bộ sẽ tăng số tin mỗi tiến trình. Chi phí thật khi chạy lớn là tiền gọi model AI, không phải máy chủ.

## Quản trị trong chat

Nhắn cho nhân viên `/admin <AI_ADMIN_TOKEN>` để trở thành quản trị viên. Sau đó dùng các lệnh:

| Lệnh | Tác dụng |
|---|---|
| `/ai show` | Xem cấu hình hiện tại |
| `/ai prompt <nội dung>` | Đổi vai trò / hướng dẫn (được nhiều dòng) |
| `/ai correct <quy tắc>`, `/ai corrections`, `/ai uncorrect <số>` | Quy tắc sửa sai |
| `/ai models`, `/ai model <tên>` | Xem các model; gán model khác (không hiện API key) |
| `/ai effort <low…max\|off>` | Đổi mức suy nghĩ (chỉ Claude) |
| `/ai skills`, `/ai skill add\|remove <tên>` | Xem, bật, tắt skill |
| `/ai routines`, `/ai run <id>`, `/ai routine pause\|resume <id>` | Lịch làm việc |
| `/ai pending`, `/ai approve <số>`, `/ai reject <số> [lý do]` | Hàng chờ duyệt |
| `/ai releases`, `/ai release <hành động>`, `/ai hold <hành động>` | Mở hoặc đóng kênh tự thực hiện |
| `/ai pause`, `/ai resume` | Tạm dừng / bật lại mọi việc (trả lời và lịch) |
| `/ai forget all` | Xoá toàn bộ trí nhớ hội thoại |
| `/ai reset` | Bỏ mọi thay đổi, quay về file cấu hình |

Thay đổi có hiệu lực ngay và được lưu trong `state_dir`, vẫn giữ sau khi khởi động lại.
Quản trị viên nhận các yêu cầu chuyển tiếp, yêu cầu chờ duyệt và báo cáo theo lịch. Khi quản trị viên
chat với nhân viên, họ dùng được cả các skill nội bộ (vd. "tóm tắt hội thoại hôm nay").
Bất kỳ ai cũng có thể gửi `/forget` để xoá lịch sử trò chuyện của chính mình.

## Quyền riêng tư

SimpleX mã hoá tin nhắn đầu-cuối giữa khách và tài khoản nhân viên. Tuy nhiên để AI trả lời,
nội dung tin nhắn, tài liệu tìm được và kết quả skill sẽ được gửi tới nhà cung cấp của model đã gán
cho nhân viên đó. Muốn dữ liệu không rời khỏi máy chủ của bạn thì dùng model chạy tại chỗ (Ollama, vLLM). Hãy thông báo
cho khách biết họ đang nói chuyện với trợ lý AI, và đừng đưa dữ liệu nhạy cảm vào tài liệu hay
skill nếu không cần thiết. Trí nhớ hội thoại, ghi chú, hàng chờ duyệt, nhật ký (`runlog.jsonl`) và
API key thêm từ giao diện (`office.json`) được lưu dạng không mã hoá trong `state_dir`; hãy bảo vệ thư mục
này. Dữ liệu gửi tới webhook của hành động đi ra hệ thống của bạn, nên chỉ khai báo địa chỉ bạn tin cậy.

Hộp thư chung (`inbox.db`) lưu bản sao mọi tin nhắn của mọi kênh, kể cả SimpleX, để nhân viên xem và trả
lời trên web: tin SimpleX không còn chỉ nằm trong ứng dụng đã mã hoá. Zalo và Facebook không mã hoá đầu-cuối;
nội dung đi qua máy chủ của các nền tảng đó.

## Kiểm thử

```bash
pip install -e '.[test]'
pytest
```

Các test dùng model giả lập (cả kiểu Claude lẫn kiểu OpenAI) nên không cần khoá API. `tests/test_e2e_simplex.py` chạy nhân viên
thật trên mạng SimpleX; đặt `SIMPLEX_TEST_SMP=smp://…` (ví dụ một SMP server cục bộ) để bật test này.
