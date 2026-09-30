"""Customer languages: detection, names, and the fixed texts customers may receive.

Detection is deliberately simple and offline: the writing system decides most
languages (Japanese, Korean, Chinese, Thai, Russian...); for Latin-script text,
Vietnamese diacritics and a few very common words decide. When unsure it returns
None and the conversation keeps the language it had (the model still mirrors the
customer, whatever this module thinks).
"""

from __future__ import annotations

import re
import unicodedata

# code -> (English name, Vietnamese name, native name)
LANGUAGES: dict[str, tuple[str, str, str]] = {
    "vi": ("Vietnamese", "Tiếng Việt", "Tiếng Việt"),
    "en": ("English", "Tiếng Anh", "English"),
    "zh": ("Chinese", "Tiếng Trung", "中文"),
    "ja": ("Japanese", "Tiếng Nhật", "日本語"),
    "ko": ("Korean", "Tiếng Hàn", "한국어"),
    "th": ("Thai", "Tiếng Thái", "ไทย"),
    "id": ("Indonesian", "Tiếng Indonesia", "Bahasa Indonesia"),
    "ms": ("Malay", "Tiếng Mã Lai", "Bahasa Melayu"),
    "km": ("Khmer", "Tiếng Khmer", "ខ្មែរ"),
    "lo": ("Lao", "Tiếng Lào", "ລາວ"),
    "fr": ("French", "Tiếng Pháp", "Français"),
    "de": ("German", "Tiếng Đức", "Deutsch"),
    "es": ("Spanish", "Tiếng Tây Ban Nha", "Español"),
    "pt": ("Portuguese", "Tiếng Bồ Đào Nha", "Português"),
    "ru": ("Russian", "Tiếng Nga", "Русский"),
    "ar": ("Arabic", "Tiếng Ả Rập", "العربية"),
    "hi": ("Hindi", "Tiếng Hindi", "हिन्दी"),
}

# Countries staff can pick for a customer, and the language to answer them in.
COUNTRIES: dict[str, tuple[str, str]] = {
    "VN": ("Việt Nam", "vi"),
    "US": ("Hoa Kỳ", "en"),
    "GB": ("Anh", "en"),
    "AU": ("Úc", "en"),
    "SG": ("Singapore", "en"),
    "PH": ("Philippines", "en"),
    "IN": ("Ấn Độ", "en"),
    "CN": ("Trung Quốc", "zh"),
    "TW": ("Đài Loan", "zh"),
    "HK": ("Hồng Kông", "zh"),
    "JP": ("Nhật Bản", "ja"),
    "KR": ("Hàn Quốc", "ko"),
    "TH": ("Thái Lan", "th"),
    "ID": ("Indonesia", "id"),
    "MY": ("Malaysia", "ms"),
    "KH": ("Campuchia", "km"),
    "LA": ("Lào", "lo"),
    "FR": ("Pháp", "fr"),
    "DE": ("Đức", "de"),
    "ES": ("Tây Ban Nha", "es"),
    "BR": ("Brazil", "pt"),
    "RU": ("Nga", "ru"),
    "AE": ("UAE", "ar"),
}


def native(code: str) -> str:
    return LANGUAGES.get(code, (code, code, code))[2]


def name(code: str | None, lang: str = "en") -> str:
    """A language's name in English (for prompts) or Vietnamese (for staff)."""
    if not code:
        return ""
    en, vi, _ = LANGUAGES.get(code, (code, code, code))
    return vi if lang == "vi" else en


# Scripts that identify a language by themselves (checked in this order).
_SCRIPTS: list[tuple[str, re.Pattern[str]]] = [
    ("ja", re.compile(r"[぀-ヿ]")),  # hiragana / katakana
    ("ko", re.compile(r"[가-힯ᄀ-ᇿ]")),
    ("zh", re.compile(r"[一-鿿]")),
    ("th", re.compile(r"[฀-๿]")),
    ("km", re.compile(r"[ក-៿]")),
    ("lo", re.compile(r"[຀-໿]")),
    ("ru", re.compile(r"[Ѐ-ӿ]")),
    ("ar", re.compile(r"[؀-ۿ]")),
    ("hi", re.compile(r"[ऀ-ॿ]")),
]
_SCRIPT_CODES = {"ja", "ko", "zh", "th", "km", "lo", "ru", "ar", "hi"}
# Letters only Vietnamese uses (â, ê, ô, á... also occur in French, Portuguese...).
_VI_LETTERS = re.compile("[ăđơưạảẹẻẽỉịọỏụủỳỵỷỹấầẩẫậắằẳẵặếềểễệốồổỗộớờởỡợứừửữự]", re.IGNORECASE)
# Frequent words, for Latin-script text without Vietnamese diacritics.
_VOCAB = {
    "vi": "khong ko k nhieu duoc dc minh giup nhe vay roi cua nao oi sdt sp bao gia mua chi anh em a",
    "en": "the is are you your do does how much what price have has can could please hello hi i my and for with this it want buy thanks",
    "fr": "bonjour je vous est le la les merci combien pour avec une des voudrais prix",
    "de": "hallo ich sie ist der die das und danke wie viel preis nicht mit bitte",
    "es": "hola el los las es por para cuanto cuánto quiero gracias precio una con",
    "pt": "olá ola obrigado obrigada quanto preço preco você voce não nao com uma",
    "id": "halo saya anda berapa harga ini itu yang dan apa bisa terima kasih mau ada",
}
_WORDS: dict[str, set[str]] = {code: set(words.split()) for code, words in _VOCAB.items()}
_TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)
_AMOUNT = re.compile(r"\d[\d.,]*\s?(?:vnđ|vnd|đ|₫|k)(?![^\W\d_])", re.IGNORECASE)


def detect(text: str, amounts: bool = False) -> str | None:
    """The language of a customer message, or None when unsure. Prices ("4.500.000đ", "500k")
    are ignored: an English message quoting one is not Vietnamese. `amounts`: count their đ
    too (a translation still writing Vietnamese prices has not left Vietnamese)."""
    text = unicodedata.normalize("NFC", text or "")
    for code, rx in _SCRIPTS:
        if rx.search(text):
            return code
    if not amounts:
        text = _AMOUNT.sub(" ", text)
    if _VI_LETTERS.search(text):
        return "vi"
    words = [w.casefold() for w in _TOKEN.findall(text)]
    if not words:
        return None
    scores = sorted(((sum(w in vocab for w in words), code) for code, vocab in _WORDS.items()), reverse=True)
    (best, code), (second, _) = scores[0], scores[1]
    # two telltale words at least: "hi", "ok" or "shop" alone say nothing about the customer
    return code if best >= 2 and best > second else None


# Fixed texts sent to customers without the model (it may be down).
TEXTS: dict[str, dict[str, str]] = {
    "busy": {
        "vi": "Xin lỗi, hệ thống đang bận, bạn thử lại sau ít phút nhé.",
        "en": "Sorry, we're busy right now. Please try again in a few minutes.",
        "zh": "抱歉，系统繁忙，请几分钟后再试。",
        "ja": "申し訳ありません。ただいま混み合っております。数分後にもう一度お試しください。",
        "ko": "죄송합니다. 지금 시스템이 바쁩니다. 몇 분 후에 다시 시도해 주세요.",
        "th": "ขออภัย ระบบไม่ว่างในขณะนี้ กรุณาลองใหม่อีกครั้งในอีกสักครู่",
        "id": "Maaf, sistem sedang sibuk. Silakan coba lagi beberapa menit lagi.",
        "ms": "Maaf, sistem sedang sibuk. Sila cuba lagi dalam beberapa minit.",
        "fr": "Désolé, le système est occupé. Veuillez réessayer dans quelques minutes.",
        "de": "Entschuldigung, das System ist gerade ausgelastet. Bitte versuchen Sie es in ein paar Minuten erneut.",
        "es": "Lo sentimos, el sistema está ocupado. Inténtelo de nuevo en unos minutos.",
        "pt": "Desculpe, o sistema está ocupado. Tente novamente em alguns minutos.",
        "ru": "Извините, система сейчас занята. Пожалуйста, попробуйте через несколько минут.",
    },
    "refusal": {
        "vi": "Xin lỗi, mình không thể hỗ trợ yêu cầu này.",
        "en": "Sorry, I can't help with this request.",
        "zh": "抱歉，我无法协助处理这个请求。",
        "ja": "申し訳ありませんが、このご依頼にはお応えできません。",
        "ko": "죄송하지만 이 요청은 도와드릴 수 없습니다.",
        "th": "ขออภัย ไม่สามารถช่วยเหลือคำขอนี้ได้",
        "id": "Maaf, saya tidak dapat membantu permintaan ini.",
        "ms": "Maaf, saya tidak dapat membantu permintaan ini.",
        "fr": "Désolé, je ne peux pas vous aider avec cette demande.",
        "de": "Entschuldigung, bei dieser Anfrage kann ich nicht helfen.",
        "es": "Lo siento, no puedo ayudar con esta solicitud.",
        "pt": "Desculpe, não posso ajudar com este pedido.",
        "ru": "Извините, я не могу помочь с этим запросом.",
    },
    "step_limit": {
        "vi": "Xin lỗi, yêu cầu này quá phức tạp để xử lý tự động.",
        "en": "Sorry, this request is too complex to handle automatically.",
        "zh": "抱歉，这个请求太复杂，无法自动处理。",
        "ja": "申し訳ありません。このご依頼は自動では対応できません。",
        "ko": "죄송합니다. 이 요청은 자동으로 처리하기에 너무 복잡합니다.",
        "th": "ขออภัย คำขอนี้ซับซ้อนเกินกว่าจะดำเนินการอัตโนมัติได้",
        "id": "Maaf, permintaan ini terlalu rumit untuk diproses secara otomatis.",
        "ms": "Maaf, permintaan ini terlalu rumit untuk diproses secara automatik.",
        "fr": "Désolé, cette demande est trop complexe pour être traitée automatiquement.",
        "de": "Entschuldigung, diese Anfrage ist zu komplex für eine automatische Bearbeitung.",
        "es": "Lo siento, esta solicitud es demasiado compleja para procesarla automáticamente.",
        "pt": "Desculpe, este pedido é complexo demais para ser tratado automaticamente.",
        "ru": "Извините, этот запрос слишком сложен для автоматической обработки.",
    },
    "request_confirmed": {
        "vi": "Yêu cầu #{id} đã được xác nhận.",
        "en": "Your request #{id} has been confirmed.",
        "zh": "您的请求 #{id} 已确认。",
        "ja": "ご依頼 #{id} が確定しました。",
        "ko": "요청 #{id}이(가) 확인되었습니다.",
        "th": "คำขอ #{id} ของคุณได้รับการยืนยันแล้ว",
        "id": "Permintaan #{id} Anda telah dikonfirmasi.",
        "ms": "Permintaan #{id} anda telah disahkan.",
        "fr": "Votre demande n°{id} a été confirmée.",
        "de": "Ihre Anfrage #{id} wurde bestätigt.",
        "es": "Su solicitud #{id} ha sido confirmada.",
        "pt": "Seu pedido #{id} foi confirmado.",
        "ru": "Ваш запрос №{id} подтверждён.",
    },
    "request_rejected": {
        "vi": "Yêu cầu #{id} chưa được chấp nhận.",
        "en": "Your request #{id} could not be accepted.",
        "zh": "您的请求 #{id} 未被接受。",
        "ja": "ご依頼 #{id} はお受けできませんでした。",
        "ko": "요청 #{id}은(는) 수락되지 않았습니다.",
        "th": "คำขอ #{id} ของคุณไม่ได้รับการอนุมัติ",
        "id": "Permintaan #{id} Anda tidak dapat diterima.",
        "ms": "Permintaan #{id} anda tidak dapat diterima.",
        "fr": "Votre demande n°{id} n'a pas pu être acceptée.",
        "de": "Ihre Anfrage #{id} konnte nicht angenommen werden.",
        "es": "Su solicitud #{id} no pudo ser aceptada.",
        "pt": "Seu pedido #{id} não pôde ser aceito.",
        "ru": "Ваш запрос №{id} не может быть принят.",
    },
}


def text(key: str, code: str | None, **values: object) -> str:
    """A fixed text in the customer's language; bilingual Vietnamese / English when unknown."""
    variants = TEXTS[key]
    if code in variants:
        return variants[code].format(**values)
    return f"{variants['vi']} / {variants['en']}".format(**values)


# Prices and product codes must survive translation exactly (small models turn "4.500.000đ"
# into "4,500,000円" or katakana-ise "MA-100"): they are swapped for placeholders first.
# Whole numbers only: not part of a longer number, a phone (090.123.4567) or a date
# (12.05.2026); one separator throughout; no leading 0 (phones, codes).
_PRICE = re.compile(
    r"(?<![\d.,])(?!0)\d{1,3}([.,])\d{3}(?:\1\d{3})*(?![.,]?\d)"
    r"(?:\s?(?:đồng|đ|vnđ|vnd)\b|\s?(?:đồng|đ|vnđ|vnd)(?=\W|$))?"
    r"|(?<![\d.,])(?!0\d)\d+(?![.,]?\d)\s?(?:đồng|vnđ|vnd)\b",
    re.IGNORECASE,
)
_CODE = re.compile(r"\b[A-Z]{1,5}-\d+[A-Za-z0-9]*(?:\s(?:Pro|Plus|Max|Mini|Lite))?\b")
# links, order/document numbers (DH00012, VIP000042) and commands the SimpleX apps make
# tappable (/orders, /'invoice DH00012') must reach the customer unchanged too
_KEEP = re.compile(r"https?://\S+|/'[^'\n]+'|(?<![\w/:.])/[a-z][a-z_]*\b|\b[A-Z]{2,4}\d{4,}\b")
_SLOT = re.compile(r"⟦\s*P\s*(\d+)\s*⟧")


def _vnd(price: str, target: str) -> str:
    """A number as the target reader should see it: 4.500.000đ -> 4,500,000 VND, 10.000 -> 10,000."""
    if target == "vi":
        return price
    digits = f"{int(re.sub(chr(92) + 'D', '', price)):,}"  # Vietnamese 10.000 is ten thousand
    return f"{digits} VND" if re.search(r"[^\d.,\s]", price) else digits


def protect(text: str, target: str) -> tuple[str, list[str]]:
    """Replace prices and product codes with ⟦P0⟧, ⟦P1⟧...; returns the text and the values."""
    values: list[str] = []

    def keep(value: str) -> str:
        values.append(value)
        return f"⟦P{len(values) - 1}⟧"

    text = _KEEP.sub(lambda m: keep(m.group(0)), text)
    text = _PRICE.sub(lambda m: keep(_vnd(m.group(0), target)), text)
    text = _CODE.sub(lambda m: keep(m.group(0)), text)
    return text, values


def restore(text: str, values: list[str]) -> tuple[str, int]:
    """Put the protected values back; returns the text and how many placeholders were lost."""
    used: set[int] = set()

    def put(m: re.Match[str]) -> str:
        i = int(m.group(1))
        if i >= len(values):
            return m.group(0)
        used.add(i)
        return values[i]

    out = _SLOT.sub(put, text)
    # a value the model wrote out itself (not as a placeholder) is not lost either
    lost = [v for i, v in enumerate(values) if i not in used and v.removesuffix(" VND") not in out]
    return out, len(lost)


_NUMBER = re.compile(r"\d+(?:[.,\s]\d{3})*")


def missing(text: str, values: list[str]) -> list[str]:
    """Protected values a translation made without placeholders dropped or changed: a price
    counts as kept in any grouping (4,500,000 / 4.500.000), a code only exactly."""
    numbers = {re.sub(r"\D", "", n) for n in _NUMBER.findall(text)}
    out = []
    for v in values:
        plain = v.removesuffix(" VND")
        if plain in text:
            continue
        if re.fullmatch(r"[\d.,\s]+", plain) and re.sub(r"\D", "", plain) in numbers:
            continue
        out.append(v)
    return out


_KANA = re.compile(r"[\u3040-\u30ff]")
_HAN = re.compile(r"[\u4e00-\u9fff]")
# Simplified Chinese characters Japanese writes differently (価, 浄, 個, 請...) or never uses.
_SIMPLIFIED_ONLY = "这们个请谢说为价净吗您呢吧对时间现实问题关并"


def is_in(text: str, code: str) -> bool:
    """Whether a translation really is in the language asked for. Unclear text passes;
    text clearly in another language (or Japanese drifting into Chinese) does not."""
    found = detect(text, amounts=True)
    if code == "ja":
        kana, han = len(_KANA.findall(text)), len(_HAN.findall(text))
        chinese = sum(text.count(c) for c in _SIMPLIFIED_ONLY)
        return found == "ja" and kana >= 0.25 * (kana + han) and chinese < 2
    if found in (None, code):
        return True
    if found == "vi" and code not in _SCRIPT_CODES:
        # Latin-script target with a few Vietnamese words kept (names, "Dạ ... ạ"): judge the rest
        words = _TOKEN.findall(text)
        vietnamese = [w for w in words if _VI_LETTERS.search(w)]
        rest = " ".join(w for w in words if not _VI_LETTERS.search(w))
        return len(vietnamese) <= 0.2 * max(1, len(words)) and detect(rest) in (None, code)
    return False


_PARTICLES = re.compile(r"^\s*(?:Dạ|dạ|Vâng|vâng)[,\s]+|\s+(?:ạ|nhé|nha)(?=\s*[.!?]|\s*$)", re.MULTILINE)


def tidy(text: str, code: str) -> str:
    """Drop Vietnamese politeness particles a model left in a translation into another language."""
    return text if code == "vi" else _PARTICLES.sub("", text).strip()
