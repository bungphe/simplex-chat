"""Customers in other countries: language detection, replies and notices in their
language, and translation for staff in the inbox."""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_operations import ORDER, Shop, order_office

from ai_employees import lang
from ai_employees.web import CSRF_HEADER, CSRF_VALUE, create_app

from fakes import ScriptedLLM, fake_chat, make_office, text, tool

H = {CSRF_HEADER: CSRF_VALUE}
PASSWORD = "correct horse battery staple"


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("Máy lọc nước MA-100 giá bao nhiêu?", "vi"),
        ("shop oi gia bao nhieu vay", "vi"),
        ("How much is the MA-100 please?", "en"),
        ("MA-100はいくらですか？", "ja"),
        ("MA-100 가격이 얼마예요?", "ko"),
        ("MA-100多少钱？", "zh"),
        ("MA-100 ราคาเท่าไหร่", "th"),
        ("Berapa harga MA-100 ini?", "id"),
        ("Bonjour, combien coûte le MA-100 ?", "fr"),
        ("Сколько стоит MA-100?", "ru"),
        ("Je voudrais une forêt", "fr"),  # â/ê/ô alone are not Vietnamese
        ("ok", None),
        ("hi", None),  # one word says nothing about the customer
        ("MA-100", None),
    ],
)
def test_detect(message, code):
    assert lang.detect(message) == code


async def test_the_customer_language_is_remembered_and_used(tmp_path):
    llm = ScriptedLLM(text("MA-100は4,500,000ドンです。"), text("はい"))
    sales = make_office(tmp_path, llm).employees["sales"]
    await sales.agent.respond(1, "Tanaka", "MA-100はいくらですか？")
    assert sales.state.language(1) == {"lang": "ja", "source": "auto"}
    situation = llm.calls[0]["system"][1]["text"]
    assert "The contact's language: Japanese." in situation
    # the operating notes tell the model to translate documents and keep prices as written
    assert "keep product names, codes, prices and currencies exactly" in llm.calls[0]["system"][0]["text"]
    # an unclear message keeps the language
    await sales.agent.respond(1, "Tanaka", "ok")
    assert sales.state.language(1)["lang"] == "ja"


async def test_fixed_texts_follow_the_customer(tmp_path):
    def down(params):
        import anthropic

        raise anthropic.APIConnectionError(request=None)  # type: ignore[arg-type]

    llm = ScriptedLLM(down, down)
    sales = make_office(tmp_path, llm).employees["sales"]
    assert await sales.agent.respond(1, "Kim", "MA-100 가격이 얼마예요?") == lang.TEXTS["busy"]["ko"]
    assert await sales.agent.respond(2, "?", "ok") == lang.text("busy", None)  # unknown: vi / en


async def test_order_notices_reach_the_customer_in_their_language(tmp_path):
    shop = Shop()
    llm = ScriptedLLM(
        tool("create_order", {"customer": "Tanaka", "items": "1 x MA-100"}),
        text("承りました"),
        tool("create_order", {"customer": "Tanaka", "items": "2 x MA-200"}),
        text("承りました"),
        lambda p: text("ご注文ありがとうございます。まもなく発送します。"),  # translated confirm_message
        lambda p: text("在庫切れです"),  # translated reason
    )
    office, sales, chat = order_office(tmp_path, llm, shop)
    await sales.command(99, "admin", "secret-token")
    await sales.agent.respond(1, "Tanaka", "MA-100を1台注文したいです")
    await sales.agent.respond(1, "Tanaka", "MA-200も2台お願いします")

    await sales.command(99, "ai", "approve 1")
    confirm = ORDER["create_order"]["confirm_message"]
    translation = llm.calls[4]
    assert "Japanese" in translation["system"][0]["text"] and translation.get("tools") in (None, [])
    assert confirm in json.dumps(translation["messages"], ensure_ascii=False)
    assert chat.sent[-1] == (1, "ご注文ありがとうございます。まもなく発送します。")

    await sales.command(99, "ai", "reject 2 hết hàng")
    assert chat.sent[-1] == (1, "ご依頼 #2 はお受けできませんでした: 在庫切れです")
    assert [r["kind"] for r in office.runlog.tail(kind="translate")] == ["translate", "translate"]


async def test_confirm_message_per_language_needs_no_translation(tmp_path):
    shop = Shop()
    actions = {
        "create_order": {
            **ORDER["create_order"],
            "confirm_message": {"vi": "Đã xác nhận đơn", "en": "Order confirmed"},
        }
    }
    llm = ScriptedLLM(tool("create_order", {"customer": "Ann", "items": "1"}), text("ok"))
    office = make_office(tmp_path, llm, http=shop.client, actions=actions, skills=["create_order"])
    sales = office.employees["sales"]
    chat = fake_chat(sales)
    await sales.command(99, "admin", "secret-token")
    await sales.agent.respond(1, "Ann", "I would like to order one MA-100 please")
    await sales.command(99, "ai", "approve 1")
    assert chat.sent[-1] == (1, "Order confirmed") and len(llm.calls) == 2


@pytest.fixture
async def client(tmp_path):
    llm = ScriptedLLM()
    office = make_office(tmp_path, llm)
    chat = fake_chat(office.employees["sales"])
    c = TestClient(TestServer(create_app(office, PASSWORD)))
    await c.start_server()
    await c.post("/api/login", json={"password": PASSWORD}, headers=H)
    yield c, office, llm, chat
    await c.close()


async def test_staff_read_and_answer_foreign_customers_in_their_own_language(client):
    c, office, llm, chat = client
    sales, hub = office.employees["sales"], office.hub
    conv, mid = hub.simplex_inbound(sales, 5, "Kim", "MA-100 가격이 얼마예요?")
    hub.inbox.set_mode(conv.id, "human")

    d = await (await c.get(f"/api/inbox/{conv.id}")).json()
    assert (d["conversation"]["lang"], d["conversation"]["lang_name"]) == ("ko", "Tiếng Hàn")

    # a customer's message, translated for staff once and kept
    llm.responses.append(text("Máy MA-100 giá bao nhiêu?"))
    r = await (await c.post(f"/api/inbox/{conv.id}/messages/{mid}/translate", json={}, headers=H)).json()
    assert r == {"translation": "Máy MA-100 giá bao nhiêu?"}
    assert "Vietnamese" in llm.calls[-1]["system"][0]["text"]
    await c.post(f"/api/inbox/{conv.id}/messages/{mid}/translate", json={}, headers=H)
    assert len(llm.calls) == 1  # cached

    # staff write Vietnamese; the customer reads Korean; the inbox keeps both
    llm.responses.append(text("MA-100은 4,500,000동입니다."))
    d = await (
        await c.post(
            f"/api/inbox/{conv.id}/reply",
            json={"text": "MA-100 giá 4.500.000đ ạ", "translate": True},
            headers=H,
        )
    ).json()
    assert chat.sent[-1] == (5, "MA-100은 4,500,000동입니다.")
    assert (d["messages"][-1]["text"], d["messages"][-1]["translation"]) == (
        "MA-100은 4,500,000동입니다.",
        "MA-100 giá 4.500.000đ ạ",
    )

    # an AI draft in the staff's language, to be translated on sending
    llm.responses.append(text("Dạ, máy còn hàng ạ."))
    r = await (await c.post(f"/api/inbox/{conv.id}/suggest", json={"staff_language": True}, headers=H)).json()
    assert r["text"] == "Dạ, máy còn hàng ạ."
    assert "in Vietnamese (it will be translated for the contact)" in llm.calls[-1]["system"][1]["text"]

    # staff know the customer lives in Japan: the choice sticks over detection
    d = await (await c.post(f"/api/inbox/{conv.id}/language", json={"country": "jp"}, headers=H)).json()
    assert (d["conversation"]["lang"], d["conversation"]["lang_source"], d["conversation"]["country"]) == (
        "ja",
        "staff",
        "JP",
    )
    hub.simplex_inbound(sales, 5, "Kim", "감사합니다")
    assert sales.state.language(5)["lang"] == "ja"
    assert (await c.post(f"/api/inbox/{conv.id}/language", json={"country": "XX"}, headers=H)).status == 400
    d = await (await c.post(f"/api/inbox/{conv.id}/language", json={}, headers=H)).json()
    assert d["conversation"]["lang"] == ""  # back to detection
    langs = await (await c.get("/api/inbox/languages")).json()
    assert {"code": "JP", "name": "Nhật Bản", "lang": "ja"} in langs["countries"]


async def test_summaries_stay_in_the_staff_language(tmp_path):
    from ai_employees.agent import SUMMARY_PROMPT

    llm = ScriptedLLM(text("Khách: Tanaka"))
    sales = make_office(tmp_path, llm).employees["sales"]
    sales.state.append_turn(1, "こんにちは", "こんにちは", keep=0)
    sales.state.data["unsummarized"]["1"] = sales.state.data["history"].pop("1")
    assert await sales.agent.summarize(1, "Tanaka")
    assert "{staff}" in SUMMARY_PROMPT
    assert (
        "Viết hoàn toàn bằng tiếng Việt, kể cả khi khách nói ngôn ngữ khác"
        in llm.calls[0]["system"][0]["text"]
    )


async def test_the_customer_language_overrides_a_vietnamese_only_role(tmp_path):
    llm = ScriptedLLM(text("The MA-100 costs 4,500,000 VND."))
    sales = make_office(tmp_path, llm, system_prompt="Luôn trả lời bằng tiếng Việt.").employees["sales"]
    await sales.agent.respond(1, "John", "How much is the MA-100 please?")
    situation = llm.calls[0]["system"][1]["text"]
    assert "Write your whole reply in English, even if your instructions above say" in situation
    assert "never convert them into another currency" in situation


async def test_translate_replies_answers_in_the_staff_language_then_translates(tmp_path):
    llm = ScriptedLLM(
        text("MA-100 giá bao nhiêu?"),  # 1. the customer's message, into Vietnamese
        text("Máy MA-100 giá 4.500.000đ, bảo hành 24 tháng."),  # 2. the answer, in Vietnamese
        text("MA-100は4,500,000ドンで、保証は24か月です。"),  # 3. the answer, into Japanese
        text("Bảo hành thế nào?"),
        text("Bảo hành 24 tháng."),
        lambda p: (_ for _ in ()).throw(RuntimeError("translation model down")),
    )
    sales = make_office(tmp_path, llm, translate_replies=True).employees["sales"]
    answer = await sales.agent.respond(1, "Tanaka", "MA-100はいくらですか？")
    assert answer == "MA-100は4,500,000ドンで、保証は24か月です。"
    question, reply, translation = llm.calls[:3]
    assert "into Vietnamese" in question["system"][0]["text"]
    assert "Write your reply in Vietnamese: it is translated into Japanese" in reply["system"][1]["text"]
    asked = json.dumps(reply["messages"], ensure_ascii=False)
    assert (
        "MA-100 giá bao nhiêu?" in asked and "The contact wrote in Japanese: MA-100はいくらですか？" in asked
    )
    assert "into Japanese" in translation["system"][0]["text"] and translation.get("tools") in (None, [])
    assert sales.state.history(1)[-1]["content"] == answer  # memory holds what the customer read
    # if translating the answer fails, the customer gets the notice in their language
    assert await sales.agent.respond(1, "Tanaka", "保証は？") == lang.TEXTS["busy"]["ja"]
    # Vietnamese customers are answered directly, with one model call
    llm.responses[:0] = [text("Dạ 4.500.000đ ạ")]
    before = len(llm.calls)
    assert await sales.agent.respond(2, "An", "MA-100 giá bao nhiêu ạ?") == "Dạ 4.500.000đ ạ"
    assert len(llm.calls) == before + 1


async def test_foreign_customers_are_told_to_search_in_the_documents_language(tmp_path):
    llm = ScriptedLLM(text("ok"))
    sales = make_office(tmp_path, llm).employees["sales"]
    await sales.agent.respond(1, "Kim", "MA-100 가격이 얼마예요?")
    assert "search them with Vietnamese keywords" in llm.calls[0]["system"][1]["text"]


def test_prices_and_codes_are_protected_in_translation():
    masked, values = lang.protect("MA-200 Pro giá 7.900.000đ, lọc 20.000L/ngày.", "ja")
    assert masked == "⟦P2⟧ giá ⟦P0⟧, lọc ⟦P1⟧L/ngày."
    assert values == ["7,900,000 VND", "20,000", "MA-200 Pro"]
    assert lang.restore("⟦P2⟧は⟦ P0 ⟧、1日⟦P1⟧L", values) == ("MA-200 Proは7,900,000 VND、1日20,000L", 0)
    assert lang.restore("MA-200 Proは⟦P0⟧", values) == ("MA-200 Proは7,900,000 VND", 1)  # 20,000 lost
    assert lang.protect("Giá 4.500.000 đồng", "vi")[1] == ["4.500.000 đồng"]  # staff keep their format


async def test_a_translation_that_loses_a_price_is_redone_unprotected(tmp_path):
    llm = ScriptedLLM(
        text("MA-100は円です"),  # dropped ⟦P0⟧ (the price)
        text("MA-100は4,500,000ドンです"),  # the plain retry
    )
    sales = make_office(tmp_path, llm).employees["sales"]
    out = await sales.agent.translate("MA-100 giá 4.500.000đ", "ja")
    assert out == "MA-100は4,500,000ドンです"
    assert "⟦P0⟧" in json.dumps(llm.calls[0]["messages"], ensure_ascii=False)
    assert "4.500.000đ" in json.dumps(llm.calls[1]["messages"], ensure_ascii=False)


async def test_a_translation_in_the_wrong_language_never_reaches_the_customer(tmp_path):
    llm = ScriptedLLM(
        text("MA-100 giá 4.500.000đ ạ"),  # "translated" into Korean, but still Vietnamese
        text("MA-100 giá 4.500.000đ ạ"),  # the retry: still Vietnamese
    )
    sales = make_office(tmp_path, llm).employees["sales"]
    with pytest.raises(Exception, match="did not produce Korean"):
        await sales.agent.translate("MA-100 giá 4.500.000đ ạ", "ko")
    assert [r["status"] for r in sales.office.runlog.tail(kind="translate")] == ["error"]
    assert not lang.is_in(
        "こんにちは！净水器MA-100的价格是4,500,000 VND并且保修24个月。", "ja"
    )  # drifted into Chinese
    assert lang.is_in("こんにちは。MA-100の価格は4,500,000 VNDです。", "ja")


async def test_translations_can_use_another_model(tmp_path):
    from fakes import OpenAIServer, oa_text

    server = OpenAIServer(oa_text("MA-100 costs 4,500,000 VND."))
    models = {
        "polyglot": {"provider": "openai", "base_url": "https://llm.local/v1", "model": "big-multilingual"}
    }
    llm = ScriptedLLM()
    sales = make_office(
        tmp_path, llm, models=models, http=server.client, translation_model="polyglot"
    ).employees["sales"]
    assert await sales.agent.translate("MA-100 giá 4.500.000đ", "en") == "MA-100 costs 4,500,000 VND."
    assert server.bodies[0]["model"] == "big-multilingual" and llm.calls == []
    from ai_employees.config import ConfigError

    with pytest.raises(ConfigError, match="translation_model 'nope'"):
        make_office(tmp_path / "x", llm, translation_model="nope")


def test_mostly_right_translations_pass_and_are_tidied():
    mixed = "Dạ chào Mia, the MA-100 is priced at 4,500,000 VND with a 24-month warranty ạ."
    assert lang.is_in(mixed, "en")
    assert (
        lang.tidy(mixed, "en") == "chào Mia, the MA-100 is priced at 4,500,000 VND with a 24-month warranty."
    )
    assert not lang.is_in("Dạ chào bạn, máy MA-100 giá 4.500.000đ ạ", "en")  # still Vietnamese
    assert lang.tidy("Dạ, em chào chị ạ.", "vi") == "Dạ, em chào chị ạ."
