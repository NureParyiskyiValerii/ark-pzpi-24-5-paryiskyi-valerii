"""
Приклади програмного коду до доповіді
«Методи рефакторингу коду програмного забезпечення» (практичне заняття № 2).

Дисципліна: Аналіз та рефакторинґ коду
Студент:    ПЗПІ-24-5, Парийський Валерій
Проєкт:     todo-suity – застосунок для керування задачами
Стек:       Python 3, FastAPI, SQLModel, SQLite

Файл містить фрагменти коду до і після застосування трьох методів
рефакторингу з каталогу М. Фаулера. Фрагменти «ДО» взято з реальної історії
проєкту, фрагменти «ПІСЛЯ» – з поточного стану backend/.

Цей файл є ілюстрацією до доповіді й не призначений для запуску.
"""

# =============================================================================
# МЕТОД 1. SEPARATE QUERY FROM MODIFIER
# (Розділення запиту та модифікатора)
#
# Суть: якщо функція одночасно повертає значення і змінює стан системи,
#       її розділяють на запит (повертає, не змінює) і команду (змінює,
#       не повертає). Основа – принцип розділення команд і запитів,
#       сформульований Б. Меєром (Command-Query Separation).
#
# Стан: проаналізовано, рефакторинг запропоновано. Не застосовано в коді,
#       бо потребує узгодженої зміни клієнтської частини.
# =============================================================================

# ---------------------------------- ДО ---------------------------------------
# backend/routers/tasks.py
#
# Обробник GET перед читанням скидає щоденні задачі, перераховує серії
# та фіксує транзакцію. Тобто запит на читання змінює базу даних.

@router.get("")
def list_tasks(session: Session = Depends(get_session)):
    today = today_str()
    ops.reset_daily(session, today)            # <-- запис
    ops.refresh_all_streaks(session, today)    # <-- запис
    session.commit()                           # <-- запис
    tasks = session.exec(
        select(Task).where(~_old_done(today)).order_by(Task.sort_order, Task.created_at)
    ).all()
    return ops.attach_subtasks(session, tasks)


# Причина такого рішення: у застосунку немає планувальника – ні фонової
# задачі, ні cron. Момент зміни доби можна помітити лише під час звернення
# клієнта, тому скидання «причепили» до першого читання.

# -------------------------------- ПІСЛЯ --------------------------------------
# Команду винесено в окремий обробник, читання стало чистим.

@router.post("/daily-refresh")
def daily_refresh(session: Session = Depends(get_session)):
    """Команда: скидає щоденні задачі та перераховує серії. Нічого не читає."""
    today = today_str()
    ops.reset_daily(session, today)
    ops.refresh_all_streaks(session, today)
    session.commit()
    return {"date": today}


@router.get("")
def list_tasks(session: Session = Depends(get_session)):
    """Запит: лише читає. Можна викликати повторно й кешувати."""
    tasks = session.exec(
        select(Task).where(~_old_done(today_str())).order_by(Task.sort_order, Task.created_at)
    ).all()
    return ops.attach_subtasks(session, tasks)


# Щоб скидання не загубилося, команда отримує два тригери.
# Перший – старт сервера: backend/main.py

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with Session(engine) as session:
        today = today_str()
        ops.reset_daily(session, today)
        ops.refresh_all_streaks(session, today)
        session.commit()
    yield


# Другий – зміна дати на боці клієнта: фронтенд уже звертається до /api/now,
# тому порівнює отриману дату зі збереженою і за розбіжності викликає
# POST /api/tasks/daily-refresh.

# Переваги:
#   * GET відповідає семантиці HTTP: безпечний, повторюваний, кешований;
#   * два паралельні читання більше не виконують скидання двічі;
#   * скидання стало окремою операцією, яку видно в журналі й можна протестувати.
# Ціна:
#   * гарантія «доба врахована» переїжджає з сервера до двох тригерів,
#     і їх треба не забути підключити.


# =============================================================================
# МЕТОД 2. REPLACE ERROR CODE WITH EXCEPTION
# (Заміна коду помилки на виняток)
#
# Суть: якщо функція сигналізує про помилку особливим значенням, яке можна
#       прийняти за дані, це значення замінюють на явну помилку. Код помилки
#       легко проігнорувати, явну помилку – ні.
#
# Стан: застосовано.
# =============================================================================

# ---------------------------------- ДО ---------------------------------------
# backend/routers/tasks.py
#
# Функція повертала 0.0 на будь-яке некоректне значення. Нуль – цілком
# правдоподібне значення для часу, тому помилку неможливо було відрізнити
# від справжніх даних.

def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0          # <-- помилка перетворюється на дані


# Виклики були розсіяні по обробнику: кожне поле приводилося окремо.
ts = int(_num(body.get("time_spent"))) if "time_spent" in body else 0
if _num(body.get("completed")) == 1:
    ...
expand = 1 if _num(body.get("expand_subtasks")) == 1 else 0


# Наслідок: некоректне значення часу тихо ставало нулем і псувало облік
# витраченого часу та статистику фокус-сесій.

# -------------------------------- ПІСЛЯ --------------------------------------
# Перевірка винесена на межу застосунку й виконується один раз для всього
# тіла запиту. Некоректне значення не перетворюється на дані – воно
# зупиняє обробку з явним повідомленням.

TASK_TYPES = ("todo", "routine", "habit")
DIFFICULTIES = ("easy", "medium", "hard", "epic")
INT_FIELDS = ("completed", "completed_today", "habit_done_today",
              "sort_order", "time_spent", "streak", "max_streak", "expand_subtasks")
DATE_FIELDS = ("date_from", "date_to", "last_done_date", "habit_last_reset")


def _validate(body: dict) -> tuple[dict, str | None]:
    """Нормалізує тіло запиту; повертає (тіло, помилка). Відсутні ключі лишаються відсутніми."""
    out = dict(body)
    if "name" in out:
        name = out["name"].strip() if isinstance(out["name"], str) else ""
        if not name:
            return out, "name required"
        out["name"] = name
    if "type" in out and out["type"] not in TASK_TYPES:
        return out, "unknown type"
    if out.get("difficulty") not in (None, "", *DIFFICULTIES):
        return out, "unknown difficulty"
    for k in INT_FIELDS:
        if k in out:
            try:
                out[k] = int(float(out[k]))
            except (TypeError, ValueError):
                return out, f"{k} must be a number"     # <-- явна помилка
    for k in DATE_FIELDS:
        v = out.get(k)
        if v not in (None, "") and not _is_date(v):
            return out, f"{k} must be YYYY-MM-DD"
    return out, None


@router.patch("/{task_id}")
def patch_task(task_id: str, body: dict = Body(default={}), session: Session = Depends(get_session)):
    body, err = _validate(body)
    if err:
        return JSONResponse(status_code=400, content={"error": err})
    ...


# Переваги:
#   * помилку неможливо прийняти за дані – вона має власний канал;
#   * клієнт отримує 400 із назвою поля замість мовчазного зіпсованого обліку;
#   * приведення типів зібране в одному місці, обробник працює з готовими значеннями.
#
# Наступний крок: 46 викликів JSONResponse зі статусом помилки у 12 модулях
# замінити на raise HTTPException і один обробник рівня застосунку – тоді
# повернення помилки зникне з бізнес-логіки повністю.


# =============================================================================
# МЕТОД 3. REPLACE TYPE CODE WITH STATE / STRATEGY
# (Заміна коду типу станом або стратегією)
#
# Суть: якщо тип-код впливає на поведінку, розгалуження за ним замінюють
#       структурою, яка зберігає поведінку кожного типу окремо.
#
# Стан: застосовано частково – розгалуження зведені до таблиці відповідності
#       та однієї функції. Повний перехід на класи-стратегії лишається
#       наступним кроком.
# =============================================================================

# ---------------------------------- ДО ---------------------------------------
# backend/routers/tasks.py
#
# Один і той самий вираз «чи виконано сьогодні» був написаний тричі.
# Третя копія вже встигла розійтися з двома іншими – у ній додалася
# перевірка дати завершення.

#  копія 1 (у перевірці серцебиття таймера)
if "time_spent" in fields and body.get("timer_started_at") and (
    (t.type == "todo" and (t.completed or 0) == 1)
    or (t.type == "routine" and (t.completed_today or 0) == 1)
    or (t.type == "habit" and (t.habit_done_today or 0) == 1)
):
    ...

#  копія 2 (у зупинці таймера)
done_now = (
    (t.type == "todo" and (t.completed or 0) == 1)
    or (t.type == "routine" and (t.completed_today or 0) == 1)
    or (t.type == "habit" and (t.habit_done_today or 0) == 1)
)

#  копія 3 (у перерахунку рядків виконання) – уже інша
done_today = (
    (t.type == "todo" and t.completed and t.completed_at
     and str(t.completed_at).startswith(today))
    or (t.type == "routine" and t.completed_today)
    or (t.type == "habit" and t.habit_done_today)
)

# Плюс правила про типи були розсіяні ще й поза обробником:
STREAK_TYPES = ("routine", "habit")                       # backend/ops.py
"... WHERE (type='routine' OR type='habit') ..."          # backend/database.py, у тексті SQL

# -------------------------------- ПІСЛЯ --------------------------------------
# 1. Таблиця відповідності: який тип яким полем позначає виконання.
#    backend/routers/tasks.py

DONE_FIELD = {"todo": "completed", "routine": "completed_today", "habit": "habit_done_today"}


# 2. Сам предикат живе в одному місці. backend/ops.py

def is_done(t: Task) -> bool:
    return bool((t.type == "todo" and t.completed)
                or (t.type == "routine" and t.completed_today)
                or (t.type == "habit" and t.habit_done_today))


# 3. Три колишні копії звелися до звернень до таблиці та виклику предиката.
#    backend/routers/tasks.py

def _guard(t: Task, body: dict) -> dict:
    """Прибирає те, що клієнт не має права записувати."""
    drop = {f for f in DONE_FIELD.values() if f != DONE_FIELD.get(t.type)}
    if t.type in ops.STREAK_TYPES:
        drop |= {"streak", "max_streak", "last_done_date"}
    if "time_spent" in body and body.get("timer_started_at"):
        toggling = DONE_FIELD.get(t.type) in body
        if (ops.is_done(t) and not toggling) or not t.timer_started_at:
            drop |= {"time_spent", "timer_started_at"}
        else:
            body = {**body, "time_spent": _heartbeat_total(t, body["time_spent"])}
    return {k: v for k, v in body.items() if k not in drop}


# і далі в обробнику:
done_field = DONE_FIELD.get(t.type)
if done_field in body:
    ops.set_done(session, t, body[done_field] == 1, today)


# Переваги:
#   * вираз існує в одному екземплярі – копії не можуть розійтися;
#   * додавання типу задачі = один запис у DONE_FIELD замість пошуку
#     усіх розгалужень вручну;
#   * обробник читається як послідовність дій, а не як набір умов.
#
# Наступний крок: замінити рядковий тип на str-Enum і винести поведінку
# кожного типу в окремий клас. Схему бази це не зачіпає: str-Enum
# зберігається тим самим рядком, тому міграція не потрібна.

class TaskType(str, Enum):
    TODO = "todo"
    ROUTINE = "routine"
    HABIT = "habit"


class RoutineBehaviour:
    done_field = "completed_today"
    tracks_streak = True
    resets_daily = True


BEHAVIOUR = {
    TaskType.TODO: TodoBehaviour(),
    TaskType.ROUTINE: RoutineBehaviour(),
    TaskType.HABIT: HabitBehaviour(),
}
