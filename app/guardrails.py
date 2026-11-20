"""
Guardrails that run in code, before any model is called (deterministic, cost no quota, can't be talked around).

1. Prompt-injection / instruction-extraction attempts are blocked with a fixed, localized reply.
   Patterns need an *instruction word* next to the verb, so normal phrasing like
   "ignore the price, show me villas" is not blocked.
2. Phone numbers and email addresses are redacted from messages before they reach an LLM provider.

Softer policies (no promised returns, no price predictions, off-topic, fair housing, no invented contact
details, treat scraped pages as data) live in the prompts in agent.py and are measured by the eval.
"""
import re

_I = re.IGNORECASE
INJECTION = [
    # English
    # needs a determiner ("previous instructions", "your rules") so "forget the rules on foreign ownership" passes
    re.compile(r"\b(ignore|disregard|forget|override|bypass)\b[^.?!\n]{0,40}\b(previous|prior|above|earlier|all|your|these|those|system|safety)\s+"
               r"(instructions?|prompts?|rules|guidelines|directives|guardrails|restrictions)\b", _I),
    re.compile(r"\b(system|developer|hidden|initial|original)\s+(prompt|message|instructions?)\b", _I),
    re.compile(r"\b(reveal|show|print|repeat|output|tell me|what are|leak|dump|share)\b[^.?!\n]{0,25}\byour\s+"
               r"(instructions|prompts?|rules|guidelines|configuration|setup)\b", _I),
    re.compile(r"\b(repeat|print|output)\b[^.?!\n]{0,20}\b(everything|all|the text)\b[^.?!\n]{0,20}\b(above|before)\b", _I),
    re.compile(r"\b(jailbreak|developer mode|DAN mode|do anything now)\b", _I),
    re.compile(r"\byou are (now|no longer)\b|\bfrom now on,? you (are|will)\b|\bnew (persona|role|instructions)\b", _I),
    # Arabic
    re.compile(r"(تجاهل|انسَ|انس|تخطَّ|تخط)[^.؟!\n]{0,30}(التعليمات|الأوامر|التوجيهات|القواعد)"),
    re.compile(r"(موجه|تعليمات|رسالة)\s+(النظام|المطور)"),
    # French
    re.compile(r"\b(ignore[zr]?|oublie[zr]?)\b[^.?!\n]{0,30}\b(instructions?|consignes|règles)\b", _I),
    re.compile(r"\bprompt\s+système\b|\binstructions\s+système\b", _I),
    # Russian
    re.compile(r"(игнорируй|игнорировать|забудь|проигнорируй)[^.?!\n]{0,30}(инструкци|правил|указани)", _I),
    re.compile(r"системн\w*\s+(промпт|подсказк|инструкци)", _I),
    # Chinese
    re.compile(r"(忽略|无视|忘记|忘掉)[^。？！\n]{0,12}(指令|指示|提示|规则|设定)"),
    re.compile(r"系统(提示|指令|设定)"),
]

REFUSAL = {
    "en": "I can't share or change how I'm set up, but I'm happy to help with Dar Global projects, Wasalt listings, "
          "prices, rental yields or investing in property. What would you like to know?",
    "ar": "لا يمكنني مشاركة إعداداتي أو تغييرها، لكن يسعدني مساعدتك في مشاريع دار جلوبال أو عقارات وصلت أو الأسعار "
          "أو العائد الإيجاري أو الاستثمار العقاري. بماذا يمكنني مساعدتك؟",
    "fr": "Je ne peux ni partager ni modifier ma configuration, mais je peux vous aider sur les projets Dar Global, "
          "les annonces Wasalt, les prix, les rendements locatifs ou l'investissement immobilier. Que souhaitez-vous savoir ?",
    "ru": "Я не могу раскрывать или менять свои настройки, но с радостью помогу с проектами Dar Global, объявлениями "
          "Wasalt, ценами, арендной доходностью или инвестициями в недвижимость. Что вас интересует?",
    "zh": "我无法透露或更改我的设定，但很乐意为您解答 Dar Global 项目、Wasalt 房源、价格、租金回报或房产投资方面的问题。"
          "您想了解什么？",
}

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# international or Saudi-style numbers: +966 55 123 4567, 0551234567, 05x-xxx-xxxx, (020) 7946 0958 ...
PHONE = re.compile(r"(?<![\w.,])(?:\+|00)?\d[\d\s().-]{7,16}\d(?![\w.,]*\d)")


def is_injection(text: str) -> bool:
    return any(p.search(text or "") for p in INJECTION)


def refusal(lang: str) -> str:
    return REFUSAL.get(lang, REFUSAL["en"])


def redact(text: str) -> tuple[str, int]:
    """Replace emails and phone-like numbers. Prices ("1,500,000", "2M") and sizes are left alone:
    a phone match needs 9+ digits and must not be comma-grouped like a price."""
    n = 0

    def phone(m):
        nonlocal n
        s = m.group(0)
        digits = re.sub(r"\D", "", s)
        if len(digits) < 9 or re.fullmatch(r"\d{1,3}(,\d{3})+", s.strip()):
            return s
        n += 1
        return "[phone removed]"

    out, k = EMAIL.subn("[email removed]", text or "")
    n += k
    return PHONE.sub(phone, out), n
