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

## Quản trị trong chat

Nhắn cho nhân viên `/admin <AI_ADMIN_TOKEN>` để trở thành quản trị viên. Sau đó dùng các lệnh:

| Lệnh | Tác dụng |
|---|---|
| `/ai show` | Xem cấu hình hiện tại |
| `/ai prompt <nội dung>` | Đổi vai trò / hướng dẫn (được nhiều dòng) |
| `/ai models` | Xem các model đã khai báo và nhân viên nào đang dùng (không hiện API key) |
| `/ai model <tên>` | Gán model khác cho nhân viên, vd. `/ai model local` |
| `/ai effort <low…max\|off>` | Đổi mức suy nghĩ |
| `/ai skills`, `/ai skill add\|remove <tên>` | Xem, bật, tắt skill |
| `/ai pause`, `/ai resume` | Tạm dừng / bật lại tự động trả lời |
| `/ai forget all` | Xoá toàn bộ trí nhớ hội thoại |
| `/ai reset` | Bỏ mọi thay đổi, quay về file cấu hình |

Thay đổi có hiệu lực ngay và được lưu trong `state_dir`, vẫn giữ sau khi khởi động lại.
Quản trị viên cũng là người nhận các yêu cầu chuyển tiếp từ `handoff_to_human`.
Bất kỳ ai cũng có thể gửi `/forget` để xoá lịch sử trò chuyện của chính mình.

## Quyền riêng tư

SimpleX mã hoá tin nhắn đầu-cuối giữa khách và tài khoản nhân viên. Tuy nhiên để AI trả lời,
nội dung tin nhắn, tài liệu tìm được và kết quả skill sẽ được gửi tới nhà cung cấp của model đã gán
cho nhân viên đó. Muốn dữ liệu không rời khỏi máy chủ của bạn thì dùng model chạy tại chỗ (Ollama, vLLM). Hãy thông báo
cho khách biết họ đang nói chuyện với trợ lý AI, và đừng đưa dữ liệu nhạy cảm vào tài liệu hay
skill nếu không cần thiết. Trí nhớ hội thoại và ghi chú được lưu dạng JSON không mã hoá trong
`state_dir`; hãy bảo vệ thư mục này.

## Kiểm thử

```bash
pip install -e '.[test]'
pytest
```

Các test dùng model giả lập (cả kiểu Claude lẫn kiểu OpenAI) nên không cần khoá API. `tests/test_e2e_simplex.py` chạy nhân viên
thật trên mạng SimpleX; đặt `SIMPLEX_TEST_SMP=smp://…` (ví dụ một SMP server cục bộ) để bật test này.
