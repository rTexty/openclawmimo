"""Классификация Telegram-сообщений."""

from brain.llm import _call_llm, _extract_json

CLASSIFY_SYSTEM = """Ты — классификатор Telegram-сообщений для CRM-системы.
Классифицируй сообщение в одну из категорий:
- noise: мусор, спам, боты, реклама
- chit-chat: личное общение, приветствия, эмодзи, не по делу
- business-small: короткие рабочие сообщения без конкретных задач
- task: явная или подразумеваемая задача, просьба, TODO
- decision: принятое решение, договорённость, согласование
- lead-signal: интерес клиента, запрос цены/условий, новый контакт
- risk: жалоба, угроза срыва, просрочка, конфликт
- other: не удалось классифицировать

Отвечай ТОЛЬКО JSON без форматирования:
{"label": "<категория>", "confidence": <0.0-1.0>, "reasoning": "<краткое пояснение>"}"""

CLASSIFY_BATCH_SYSTEM = """Ты — классификатор Telegram-сообщений для CRM-системы.
Классифицируй КАЖДОЕ сообщение из списка в одну из категорий:
- noise: мусор, спам, боты, реклама
- chit-chat: личное общение, приветствия, эмодзи, не по делу
- business-small: короткие рабочие сообщения без конкретных задач
- task: явная или подразумеваемая задача, просьба, TODO
- decision: принятое решение, договорённость, согласование
- lead-signal: интерес клиента, запрос цены/условий, новый контакт
- risk: жалоба, угроза срыва, просрочка, конфликт
- other: не удалось классифицировать

Отвечай СТРОГО JSON array без форматирования, ровно N элементов:
[{"label":"<категория>","confidence":<0.0-1.0>,"reasoning":"<кратко>"}, ...]

Количество элементов ДОЛЖНО совпадать с количеством сообщений."""


def classify_message(text, chat_context=None):
    """Классифицировать сообщение. Возвращает (label, confidence, reasoning)."""
    context_str = f"\nКонтекст чата: {chat_context}" if chat_context else ""
    result = _call_llm(CLASSIFY_SYSTEM, f"Сообщение:\n{text}{context_str}")

    if result:
        try:
            data = _extract_json(result)
            if isinstance(data, dict):
                return (
                    data.get("label", "other"),
                    data.get("confidence", 0.5),
                    data.get("reasoning", ""),
                )
        except Exception:
            pass

    return _classify_heuristic(text)


def classify_batch(texts: list[str], chat_contexts: list[str] | None = None):
    """
    Batch-классификация: N сообщений → 1 LLM-вызов.
    Возвращает list[(label, confidence, reasoning)].

    Экономия: ~60% токенов по сравнению с classify_message() в цикле.
    При недоступности LLM — fallback на _classify_heuristic поштучно.
    """
    if not texts:
        return []

    if chat_contexts is None:
        chat_contexts = [""] * len(texts)

    # Формируем numbered list
    numbered = []
    for i, (text, ctx) in enumerate(zip(texts, chat_contexts), 1):
        ctx_str = f" [контекст: {ctx}]" if ctx else ""
        numbered.append(f"{i}. {text}{ctx_str}")

    messages_block = "\n".join(numbered)
    result = _call_llm(
        CLASSIFY_BATCH_SYSTEM,
        f"Сообщения ({len(texts)} штук):\n{messages_block}",
        temperature=0.0,
        max_tokens=1024,
    )

    if result:
        try:
            data = _extract_json(result)
            if isinstance(data, list):
                parsed = [
                    (
                        item.get("label", "other"),
                        item.get("confidence", 0.5),
                        item.get("reasoning", "batch"),
                    )
                    for item in data
                ]
                if len(parsed) == len(texts):
                    return parsed
                # Partial: LLM вернул меньше элементов — используем что есть, остальные heuristic
                if 0 < len(parsed) < len(texts):
                    parsed += [_classify_heuristic(t) for t in texts[len(parsed) :]]
                    return parsed
        except Exception:
            pass

    # Fallback: heuristic поштучно
    return [_classify_heuristic(t) for t in texts]


def _classify_heuristic(text):
    """Эвристическая классификация без LLM."""
    text_lower = text.lower()

    # Noise
    noise_words = [
        "подписывайтесь",
        "реклама",
        "скидка",
        "акция",
        "бесплатно",
        "промокод",
    ]
    if any(w in text_lower for w in noise_words):
        return ("noise", 0.6, "heuristic: noise keywords")

    # Risk
    risk_words = [
        "задержка",
        "просроч",
        "жалоб",
        "не могу дозвониться",
        "где деньги",
        "конфликт",
    ]
    if any(w in text_lower for w in risk_words):
        return ("risk", 0.7, "heuristic: risk keywords")

    # Lead signal
    lead_words = [
        "сколько стоит",
        "какая цена",
        "хочу заказать",
        "интересует",
        "можете сделать",
        "кп",
        "коммерческое",
        "предоплат",
        "соглас",
    ]
    if any(w in text_lower for w in lead_words):
        return ("lead-signal", 0.7, "heuristic: lead keywords")

    # Decision
    decision_words = ["согласен", "договорились", "подтверждаю", "одобряю", "решили"]
    if any(w in text_lower for w in decision_words):
        return ("decision", 0.7, "heuristic: decision keywords")

    # Task
    task_words = [
        "сделай",
        "сделать",
        "нужно",
        "надо",
        "пришли",
        "отправь",
        "подготовь",
        "созвон",
    ]
    if any(w in text_lower for w in task_words):
        return ("task", 0.6, "heuristic: task keywords")

    # Short messages usually chit-chat
    if len(text) < 20:
        return ("chit-chat", 0.5, "heuristic: short message")

    return ("business-small", 0.4, "heuristic: default")
