---
name: lenochka-crm
description: "CRM-операции Lenochka: управление контактами, сделками, лидами, задачами, договорами, счетами. Запись и обновление бизнес-сущностей. Запросы к CRM-данным."
metadata.openclaw.requires:
  env:
    - LENOCHKA_DB_PATH
  bins: ["python3", "sqlite3"]
---

# Lenochka CRM — Полный скилл

Управление всеми CRM-сущностями. Создание, обновление, запросы. Мост между extracted entities и структурированными данными.

---

## 🛠 ИНСТРУМЕНТЫ

```bash
python3 run_crm.py <команда> [аргументы]
```

---

### 1. CONTACT — Управление контактами

```bash
# Найти контакт по Telegram username
python3 run_crm.py contact --find --tg_username ivan_petrov

# Найти по tg_user_id (Telegram numeric ID)
python3 run_crm.py contact --find --tg_user_id 123456789

# Найти по ID
python3 run_crm.py contact --find --id 42

# Создать новый контакт
python3 run_crm.py contact --create --name "Иван Петров" --tg_username ivan_petrov --tg_user_id 123456789

# Обновить контакт
python3 run_crm.py contact --update --id 42 --name "Иван Петров" --company "ООО Ромашка" --phones '["+79991234567"]'

# Upsert (найти или создать) — основной сценарий
python3 run_crm.py contact --upsert --tg_user_id 123456789 --name "Иван Петrov" --tg_username ivan_petrov
```

**Правила upsert:**
1. Искать по `tg_user_id` (если передан)
2. Если не найден → искать по `tg_username`
3. Если не найден → создать новый
4. Если найден → обновить name (если передан и не пустой)

---

### 2. DEAL — Сделки

```bash
# Создать сделку
python3 run_crm.py deal --create --contact_id 42 --amount 150000 --stage discovery

# Обновить сумму
python3 run_crm.py deal --update --contact_id 42 --amount 200000

# Закрыть сделку (won)
python3 run_crm.py deal --close --contact_id 42 --stage closed_won

# Закрыть (lost)
python3 run_crm.py deal --close --contact_id 42 --stage closed_lost

# Найти активные сделки контакта
python3 run_crm.py deal --find --contact_id 42
```

**Стадии:** `discovery` → `proposal` → `negotiation` → `contract` → `closed_won` / `closed_lost`

**Правила upsert:**
1. Искать активную сделку WHERE contact_id=? AND stage NOT IN ('closed_won','closed_lost')
2. Если есть → обновить (amount если новая больше, stage если явно указан)
3. Если нет → создать новую (stage=discovery)

---

### 3. TASK — Задачи

```bash
# Создать задачу
python3 run_crm.py task --create \
  --description "Пришлю КП завтра" \
  --due_date "2026-04-01" \
  --priority normal \
  --contact_id 42

# Обновить статус
python3 run_crm.py task --update --id 15 --status done
python3 run_crm.py task --update --id 15 --status in_progress
python3 run_crm.py task --update --id 15 --status cancelled

# Продлить дедлайн
python3 run_crm.py task --update --id 15 --due_date "2026-04-05"

# Найти открытые задачи
python3 run_crm.py task --find --status open

# Найти задачи контакта
python3 run_crm.py task --find --contact_id 42 --status open
```

**Приоритеты:** `low`, `normal`, `high`, `urgent`

**Правила:**
- Всегда указывай `--contact_id` если задача привязана к клиенту
- `--due_date` — точная дата (YYYY-MM-DD). Если "завтра" — вычисли сам.
- `--source_message_id` — для дедупликации

---

### 4. LEAD — Лиды

```bash
# Создать лид
python3 run_crm.py lead --create --contact_id 42 --source telegram --amount 150000 --probability 0.5

# Обновить статус
python3 run_crm.py lead --update --contact_id 42 --status contacted
python3 run_crm.py lead --update --contact_id 42 --status won
python3 run_crm.py lead --update --contact_id 42 --status lost

# Найти активные лиды
python3 run_crm.py lead --find --active
```

**Статусы:** `new` → `contacted` → `qualified` → `proposal` → `negotiation` → `won` / `lost`

---

### 5. AGREEMENT — Договоры

```bash
# Создать договор
python3 run_crm.py agreement --create --contact_id 42 --summary "Договор на разработку" --amount 150000 --due_date "2026-04-15"

# Обновить статус
python3 run_crm.py agreement --update --id 10 --status signed
python3 run_crm.py agreement --update --id 10 --status completed
```

**Статусы:** `draft` → `sent` → `signed` → `completed` / `cancelled`

---

### 6. QUERY — Запросы к CRM

```bash
# Активные сделки
python3 run_crm.py query --active-deals

# Открытые задачи
python3 run_crm.py query --open-tasks --limit 10

# Просроченные задачи
python3 run_crm.py query --overdue-tasks

# Брошенные диалоги (>N часов)
python3 run_crm.py query --abandoned --hours 48

# Активные лиды
python3 run_crm.py query --active-leads

# Дневная сводка
python3 run_crm.py query --daily-summary --date 2026-03-31

# Контакт + его сделки + задачи (полная картина)
python3 run_crm.py query --contact-full --contact_id 42

# Upcoming deadlines (ближайшие N дней)
python3 run_crm.py query --upcoming --days 3
```

---

## 📋 ИНСТРУКЦИЯ: КАК МАППИТЬ ENTITIES В CRM

После `run_memory.py ingest` я получаю `entities` (contact, amounts, dates, task, deal, lead, agreement).

### Маппинг:

```
entities.contact (name, tg_username)
  → run_crm.py contact --upsert --tg_username ... --name ...

entities.amounts[] + contact_id
  → run_crm.py deal --update --contact_id N --amount MAX(amounts)

entities.task (description, due_date, priority)
  → run_crm.py task --create --contact_id N --description ... --due_date ...

entities.lead (source, amount, probability)
  → run_crm.py lead --create --contact_id N --source telegram --amount ...

entities.agreement (summary, amount, due_date)
  → run_crm.py agreement --create --contact_id N --summary ... --amount ... --due_date ...
```

### Пример полного маппинга:

```
Сообщение: "Клиент Иван согласился на 150к, договор до 15 апреля, пришлю КП завтра"
Entities: {
  "contact": {"name": "Иван", "tg_username": null},
  "amounts": [150000],
  "dates": ["2026-04-15"],
  "task": {"description": "Пришлю КП", "due_date": "2026-04-01"}
}

Действия:
1. contact --upsert --name "Иван"        → contact_id=42
2. deal --update --contact_id 42 --amount 150000
3. agreement --create --contact_id 42 --due_date "2026-04-15" --amount 150000
4. task --create --contact_id 42 --description "Пришлю КП" --due_date "2026-04-01"
```

---

## ⚠️ EDGE CASES

| Случай | Что делать |
|--------|-----------|
| Contact уже существует | upsert: обновить name если новый не пустой |
| Сделка уже активна | upsert: обновить amount/stage, не создавать дубль |
| Нет contact_id для задачи | related_type='other', related_id=NULL |
| due_date = "завтра" | Вычислить: today + 1 day |
| due_date = "в пятницу" | Вычислить: ближайшая пятница |
| Сумма "снизить на 50к" | Это delta, не абсолютная. Не обновлять deal.amount напрямую |
| "Не 150, а 120" | Supersede: обновить deal.amount = 120 |
| FK constraint failed | Не падать. Создать contact сначала, потом retry |
