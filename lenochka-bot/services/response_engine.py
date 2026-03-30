"""
Response Engine — ядро генерации ответов и роутинга.

Архитектура:
  Combined classify+route: ОДИН LLM-вызов на батч (классификация + решение что делать)
  Fact response: SQL → LLM формулировка
  Escalation: → owner notification через notifier
  Follow-up detection: LLM ищет implicit obligations

Anti-loop: ResponseGuard per chat_thread (max 3 consecutive + cooldown).
"""
import re
import time
import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("lenochka.response")


# =========================================================
# 1. RESPONSE GUARD — anti-loop + anti-spam
# =========================================================

class ResponseGuard:
    """
    Защита от спам-ответов и петель.
    State per chat_thread: последний ответ, счётчик, cooldown.
    """

    def __init__(self, min_interval: float = 180.0,
                 max_consecutive: int = 3,
                 cooldown_seconds: float = 900.0):
        self.min_interval = min_interval
        self.max_consecutive = max_consecutive
        self.cooldown_seconds = cooldown_seconds
        self._last_response: dict[int, float] = {}
        self._consecutive: dict[int, int] = {}
        self._cooldown_until: dict[int, float] = {}

    def can_respond(self, chat_thread_id: int) -> tuple[bool, str]:
        now = time.time()

        # Circuit breaker
        if chat_thread_id in self._cooldown_until:
            if now < self._cooldown_until[chat_thread_id]:
                return False, "cooldown"
            del self._cooldown_until[chat_thread_id]
            self._consecutive[chat_thread_id] = 0

        # Min interval
        last = self._last_response.get(chat_thread_id, 0)
        if now - last < self.min_interval:
            return False, "too_soon"

        # Max consecutive
        if self._consecutive.get(chat_thread_id, 0) >= self.max_consecutive:
            self._cooldown_until[chat_thread_id] = now + self.cooldown_seconds
            return False, "max_consecutive"

        return True, "ok"

    def record_response(self, chat_thread_id: int):
        now = time.time()
        self._last_response[chat_thread_id] = now
        self._consecutive[chat_thread_id] = self._consecutive.get(chat_thread_id, 0) + 1

    def reset_if_idle(self, chat_thread_id: int, idle_seconds: float = 600.0):
        """Сбросить счётчик если прошло достаточно времени."""
        last = self._last_response.get(chat_thread_id, 0)
        if time.time() - last > idle_seconds:
            self._consecutive[chat_thread_id] = 0


# Global instance
response_guard = ResponseGuard()


# =========================================================
# 2. COMBINED CLASSIFY + ROUTE (primary architecture)
# =========================================================

COMBINED_SYSTEM = """Ты — классификатор и роутер сообщений для CRM-системы Lenochka.

Для КАЖДОГО сообщения определи:

1. Классификация (label):
   noise / chit-chat / business-small / task / decision / lead-signal / risk / other

2. Действие (action):
   - "respond_fact" — клиент спрашивает, можно ответить ТОЛЬКО из данных CRM
   - "escalate" — вопрос требует решения владельца (цены, КП, встречи, жалобы)
   - "skip" — не требует ответа (ок, согласен, эмоции, спам)

3. Если respond_fact — укажи intent и что искать (query_hint):
   - "deadline" — когда будет готово? сроки?
   - "status" — что там с проектом/задачей?
   - "amount" — сколько договорились? какая сумма?
   - "context_recall" — напомни о чём говорили
   - "payment_status" — что с оплатой? оплатил?
   - "overdue" — что просрочено? что зависло?
   - "tasks_today" — что на сегодня?
   - "active_leads" — сколько лидов?
   - "deal_details" — детали сделки (сумма, стадия)
   - "contact_history" — что знаем о клиенте?
   - "last_interaction" — когда последний раз общались?

4. Для escalate — укажи тип:
   pricing / proposal / contract / meeting / complaint / other

Отвечай СТРОГО JSON array, ровно N элементов:
[{"label":"<категория>","confidence":0.0-1.0,"action":"respond_fact|escalate|skip",
  "intent":"<тип или null>","query_hint":"<что искать или null>",
  "escalation_type":"<тип или null>","reason":"<кратко>"}]"""


def classify_and_route_batch(texts: list[str], chat_contexts: list[str],
                              brain) -> list[dict]:
    """
    ОДИН LLM-вызов: классификация + роутинг для N сообщений.
    Возвращает list[{label, confidence, action, intent, query_hint, escalation_type, reason}].
    """
    if not texts:
        return []

    numbered = []
    for i, (text, ctx) in enumerate(zip(texts, chat_contexts), 1):
        ctx_str = f" [контекст: {ctx}]" if ctx else ""
        numbered.append(f"{i}. {text}{ctx_str}")

    messages_block = "\n".join(numbered)

    if brain.is_ready():
        try:
            result = brain._call_llm(
                COMBINED_SYSTEM,
                f"Сообщения ({len(texts)} штук):\n{messages_block}",
                temperature=0.0,
                max_tokens=2048,
            )
            if result:
                data = brain._extract_json(result)
                if isinstance(data, list):
                    if len(data) == len(texts):
                        return _normalize_decisions(data)
                    if 0 < len(data) < len(texts):
                        return _normalize_decisions(data) + _fallback_decisions(
                            texts[len(data):], "partial LLM response"
                        )
        except Exception as e:
            logger.warning(f"Combined classify+route failed: {e}")

    # Fallback: heuristic classify + escalate
    return _fallback_decisions(texts, "LLM unavailable")


def _normalize_decisions(raw: list) -> list[dict]:
    """Нормализовать решения LLM — гарантировать наличие всех полей."""
    result = []
    for d in raw:
        if not isinstance(d, dict):
            result.append(_default_decision())
            continue
        result.append({
            "label": d.get("label", "other"),
            "confidence": float(d.get("confidence", 0.5)),
            "action": d.get("action", "escalate"),
            "intent": d.get("intent"),
            "query_hint": d.get("query_hint"),
            "escalation_type": d.get("escalation_type", d.get("intent", "other")),
            "reason": d.get("reason", ""),
        })
    return result


def _default_decision() -> dict:
    return {"label": "other", "confidence": 0.3, "action": "escalate",
            "intent": "other", "query_hint": None, "escalation_type": "other",
            "reason": "parse error"}


def _fallback_decisions(texts: list[str], reason: str) -> list[dict]:
    from .brain_wrapper import BrainWrapper
    decisions = []
    for t in texts:
        try:
            from brain import _classify_heuristic
            label, conf, _ = _classify_heuristic(t)
        except Exception:
            label, conf = "other", 0.3
        decisions.append({
            "label": label, "confidence": conf,
            "action": "escalate" if label not in ("noise", "chit-chat") else "skip",
            "intent": "other", "query_hint": None,
            "escalation_type": "other", "reason": reason,
        })
    return decisions


# =========================================================
# 3. FACT RESPONSE GENERATION
# =========================================================

RESPONSE_GEN_SYSTEM = """Ты — Lenochka, AI-ассистент владельца бизнеса.
Клиент задал вопрос. У тебя есть факты из CRM.

ПРАВИЛА:
- Отвечай ТОЛЬКО на основе фактов. Не выдумывай.
- 1-3 предложения. Деловой тон.
- Не упоминай "CRM", "база данных", "система".
- Отвечай на языке клиента.
- Если фактов мало — кратко. Не растекайся."""


def generate_fact_response(question: str, facts: str,
                            contact_name: str, brain) -> str | None:
    """
    LLM формулирует естественный ответ на основе SQL-фактов.
    ОДИН вызов. ~$0.001.
    """
    if not brain.is_ready():
        return None

    user_prompt = (
        f"Вопрос клиента: {question}\n\n"
        f"Контакт: {contact_name}\n\n"
        f"Факты из CRM:\n{facts}"
    )

    try:
        result = brain._call_llm(
            RESPONSE_GEN_SYSTEM, user_prompt,
            temperature=0.3, max_tokens=200,
        )
        if result and len(result) > 5:
            return result[:500]
    except Exception as e:
        logger.warning(f"Response generation failed: {e}")

    return None


# =========================================================
# 4. DIALOG ENDED DETECTION — fast path + LLM
# =========================================================

EXACT_END_PHRASES = {
    "ок", "окей", "ok", "okay", "хорошо", "ладно",
    "согласен", "согласна", "согласовано", "подтверждаю",
    "договорились", "принято", "отлично", "понял", "поняла",
    "понятно", "ясно", "угу", "ага", "逴",
}


def fast_dialog_ended(text: str) -> bool:
    """Бесплатная проверка: точные фразы. LLM не нужен."""
    normalized = text.lower().strip().rstrip(".!?,;:😴👍🤝✅👌🤚")
    return normalized in EXACT_END_PHRASES


def fast_sticker_ended(msg) -> bool:
    """Стикер-подтверждение. Бесплатно."""
    if not hasattr(msg, 'sticker') or not msg.sticker:
        return False
    emoji = msg.sticker.emoji or ""
    return emoji in ("👍", "🤝", "✅", "👌", "👊", "💯")


# =========================================================
# 5. FOLLOW-UP DETECTION
# =========================================================

FOLLOWUP_DETECT_SYSTEM = """Ты — детектор договорённостей и follow-up обязательств.
Из сообщения извлеки все договорённости, сроки и обязательства.

Примеры:
- "Договорились, пришлю КП в среду" → obligation: "пришлёт КП", due: среда, who: owner
- "Клиент сказал оплатит до пятницы" → obligation: "клиент оплатит", due: пятница, who: client
- "Напишу ему завтра" → obligation: "написать клиенту", due: завтра, who: owner
- "Подожди до конца месяца" → obligation: "ждать", due: конец месяца, who: owner
- "Ладно, тогда в понедельник обсудим" → obligation: "обсудить", due: понедельник, who: both
- "Ничего не договорились, просто привет" → нет obligations

Верни JSON array (может быть несколько или пустой):
[{"obligation":"<что сделать>","who":"owner|client|both",
  "due_hint":"<естественное описание срока>","due_date":"YYYY-MM-DD или null",
  "reminder_days_before":2}]

Если нет договорённостей — пустой массив []."""


def detect_followups(text: str, chat_context: str, brain) -> list[dict]:
    """
    LLM ищет implicit obligations в тексте.
    Не вызывается для noise/chit-chat.
    """
    if not brain.is_ready():
        return []

    try:
        result = brain._call_llm(
            FOLLOWUP_DETECT_SYSTEM,
            f"Сообщение:\n{text}\n\nКонтекст чата:\n{chat_context}",
            temperature=0.0, max_tokens=500,
        )
        if result:
            data = brain._extract_json(result)
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict) and "obligation" in d]
    except Exception as e:
        logger.warning(f"Follow-up detection failed: {e}")

    return []


# =========================================================
# 6. PROGRESS CHECK-IN REPLY — LLM-based
# =========================================================

PROGRESS_REPLY_SYSTEM = """Ты — помощник владельца бизнеса в CRM-системе Lenochka.
Owner ответил на check-in сообщение о задаче. Определи что делать с задачей.

Задача: {task_description}
Дедлайн: {due_at}
Текущий статус: {status}

Ответ owner'а: {owner_reply}

Определи action (один из):
- "done" — задача выполнена
- "in_progress" — в работе, продвигается
- "extend" — нужно продлить срок (укажи new_date или extend_days)
- "blocked" — заблокирована, есть препятствие
- "cancel" — задача отменена
- "remind_tomorrow" — напомнить завтра
- "remind_date" — напомнить в конкретную дату
- "escalate" — проблема, нужно вмешательство
- "update" — просто обновление статуса, без изменения задачи

Отвечай ТОЛЬКО JSON:
{"action":"<тип>","new_date":"YYYY-MM-DD или null","extend_days":N или null,
 "notes":"<что сказал owner, кратко>","priority":"<low|normal|high|urgent или null>"}
"""


def parse_progress_reply(text: str, task: dict, brain) -> dict:
    """
    LLM понимает ответ owner'а и определяет что делать с задачей.
    """
    if not brain.is_ready():
        return {"action": "update", "new_date": None, "extend_days": None,
                "notes": text, "priority": None}

    try:
        result = brain._call_llm(
            PROGRESS_REPLY_SYSTEM.format(
                task_description=task.get("description", "?"),
                due_at=task.get("due_at", "не указан"),
                status=task.get("status", "open"),
                owner_reply=text,
            ),
            "", temperature=0.0, max_tokens=300,
        )
        if result:
            data = brain._extract_json(result)
            if isinstance(data, dict) and "action" in data:
                return data
    except Exception as e:
        logger.warning(f"Progress reply parse failed: {e}")

    # Fallback — записать как notes
    return {"action": "update", "new_date": None, "extend_days": None,
            "notes": text, "priority": None}


def format_progress_confirmation(decision: dict) -> str:
    """Форматировать подтверждение обновления задачи."""
    action = decision.get("action", "update")
    notes = decision.get("notes", "")
    suffix = f" — {notes}" if notes else ""

    if action == "done":
        return f"✅ Задача закрыта{suffix}"
    elif action == "in_progress":
        return f"🔨 В работе{suffix}"
    elif action == "extend":
        if decision.get("new_date"):
            return f"📅 Дедлайн → {decision['new_date']}{suffix}"
        days = decision.get("extend_days", 3)
        return f"📅 Дедлайн продлён на {days}д{suffix}"
    elif action == "blocked":
        return f"⚠️ Заблокировано (urgent){suffix}"
    elif action == "cancel":
        return f"❌ Задача отменена{suffix}"
    elif action == "remind_tomorrow":
        return f"🔔 Напомню завтра"
    elif action == "remind_date":
        return f"🔔 Напомню {decision.get('new_date', '?')}"
    elif action == "escalate":
        return f"🚨 Эскалировано{suffix}"
    return f"📝 Записал{suffix}"


# =========================================================
# 7. NOTIFICATION FORMATTING
# =========================================================

ESCALATION_TEMPLATES = {
    "pricing": "💰 {contact_name} спрашивает о цене ({wait_time})\n\n💬 «{message_text}»\n\n📋 Контекст:\n{context_block}\n\n💡 Напишите клиенту напрямую.",
    "proposal": "📄 {contact_name} просит КП ({wait_time})\n\n💬 «{message_text}»\n\n📋 {context_block}",
    "contract": "📝 {contact_name} спрашивает о договоре ({wait_time})\n\n💬 «{message_text}»\n\n📋 {context_block}",
    "meeting": "📅 {contact_name} предлагает встречу ({wait_time})\n\n💬 «{message_text}»\n\n📋 {context_block}",
    "complaint": "⚠️ {contact_name}: жалоба/риск ({wait_time})\n\n💬 «{message_text}»\n\n📋 {context_block}",
    "other": "❓ {contact_name} спрашивает ({wait_time})\n\n💬 «{message_text}»\n\n📋 {context_block}",
}


def format_escalation_notification(escalation_type: str, contact_name: str,
                                    message_text: str, wait_time: str,
                                    context_block: str = "") -> str:
    """Сформировать текст уведомления owner'у об эскалации."""
    template = ESCALATION_TEMPLATES.get(escalation_type, ESCALATION_TEMPLATES["other"])
    return template.format(
        contact_name=contact_name or "Клиент",
        message_text=message_text[:200] if message_text else "",
        wait_time=wait_time,
        context_block=context_block or "нет дополнительного контекста",
    )


def format_duration(seconds: float) -> str:
    """Человекочитаемая длительность."""
    if seconds < 60:
        return f"{int(seconds)}с"
    if seconds < 3600:
        return f"{int(seconds // 60)}мин"
    hours = seconds / 3600
    if hours < 24:
        return f"{hours:.1f}ч"
    return f"{hours / 24:.1f}д"
