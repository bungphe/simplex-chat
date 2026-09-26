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
                                        │ knowledge_search, order_status, notes…
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
| `notes` (`remember` + `recall`) | Ghi nhớ thông tin về từng khách (số điện thoại, nhu cầu…) |
| `ask_colleague` | Hỏi nhân viên AI khác; giới hạn bằng `skill_config.ask_colleague.colleagues` |
| `handoff_to_human` | Chuyển yêu cầu cho quản trị viên (người thật) qua SimpleX |
| `current_time` | Ngày giờ hiện tại theo múi giờ |
| `web_search` | Tìm kiếm web (chạy trên máy chủ Anthropic, chỉ với model Claude) |
| `recent_conversations` | *Nội bộ*: đọc hội thoại gần đây của chính nhân viên, cho báo cáo |
| `office_report` | *Nội bộ*: tình hình cả văn phòng (hoạt động, lỗi, chờ duyệt, lịch) |
| hành động (vd. `create_order`) | Gọi webhook vào hệ thống của bạn, có hàng chờ duyệt (xem bên dưới) |

Skill *nội bộ* chỉ được dùng khi chạy lịch làm việc hoặc khi người chat là quản trị viên. Khách bình
thường không bao giờ khiến nhân viên đọc được hội thoại của người khác.

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

Mở `http://127.0.0.1:8080` và đăng nhập bằng mật khẩu. Giao diện gồm các trang:

| Trang | Làm được gì |
|---|---|
| Tổng quan | Trạng thái từng nhân viên, số việc 24 giờ, yêu cầu chờ duyệt, lịch tiếp theo, địa chỉ SimpleX |
| Nhân viên | Sửa vai trò, gán model, mức suy nghĩ, bật/tắt skill, mở kênh hành động, quy tắc sửa sai, chạy/tạm dừng lịch, gỡ quản trị viên, khôi phục cấu hình gốc |
| Chờ duyệt | Duyệt hoặc từ chối yêu cầu của mọi nhân viên, xem lịch sử |
| Model AI | Xem model, **thêm model mới kèm API key**, thử kết nối, xoá |
| Hội thoại | Xem hội thoại và ghi chú về từng khách, xoá trí nhớ |
| Nhật ký | Mọi việc đã làm, lọc theo nhân viên và loại việc |

Bảo mật của giao diện:
- Đăng nhập bằng mật khẩu (nên dài ít nhất 12 ký tự). Phiên đăng nhập nằm trong cookie `HttpOnly`, `SameSite=Strict`.
- Mọi thao tác thay đổi đều cần một header riêng mà trang web khác không gửi được (chống CSRF).
- API key chỉ nhập vào được, không bao giờ hiện lại. Key thêm từ giao diện được lưu trong `state_dir/office.json` với quyền `600`.
- Muốn truy cập từ xa, đặt giao diện sau reverse proxy có HTTPS. Đừng mở cổng trực tiếp ra Internet.

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

## Kiểm thử

```bash
pip install -e '.[test]'
pytest
```

Các test dùng model giả lập (cả kiểu Claude lẫn kiểu OpenAI) nên không cần khoá API. `tests/test_e2e_simplex.py` chạy nhân viên
thật trên mạng SimpleX; đặt `SIMPLEX_TEST_SMP=smp://…` (ví dụ một SMP server cục bộ) để bật test này.
