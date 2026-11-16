import json
import secrets
import time
import uuid

from django.conf import settings
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST
from dotenv import dotenv_values
from google import genai

from .models import SavedRecipe
from .catalog import CATEGORIES, PRODUCTS, build_products

PENDING_TTL_SECONDS = 15 * 60  # 15 min


class _DuplicateKeyError(Exception):
    pass


def _safe_language(value):
    return value if value in ("kk", "ru") else "kk"


# ═════════ JSON validator (AI жауабын сенімсіз дерек ретінде тексереді) ═════════

def _reject_duplicates(pairs):
    seen = set()
    for k, _ in pairs:
        if k in seen:
            raise _DuplicateKeyError()
        seen.add(k)
    return dict(pairs)


def _strip_code_fences(text):
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    if text.endswith("```"):
        text = text[:-3].rstrip()
    return text


def validate_recipe_payload(text):
    """
    Қатаң JSON тексеру. Қайтарады: (dict, None) немесе (None, error_key).
    Артық/қайталанған кілт, бос/қате тип қабылданбайды.
    Шектер (бұрынғы шектер): title 3–200, ingredients 1–30 × 1–200, steps 1–30 × 1–500.
    """
    if not text:
        return None, "empty"
    cleaned = _strip_code_fences(text)
    try:
        data = json.loads(cleaned, object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        return None, "duplicate_keys"
    except (json.JSONDecodeError, ValueError):
        return None, "invalid_json"

    if not isinstance(data, dict):
        return None, "not_object"

    expected = {"title", "ingredients", "steps"}
    if set(data.keys()) != expected:
        return None, "wrong_keys"

    title = data.get("title")
    if not isinstance(title, str):
        return None, "bad_title"
    title = title.strip()
    if not (3 <= len(title) <= 200) or "\n" in title or "\r" in title:
        return None, "bad_title"

    ingredients = data.get("ingredients")
    if not isinstance(ingredients, list) or not (1 <= len(ingredients) <= 30):
        return None, "bad_ingredients"
    clean_ing = []
    for item in ingredients:
        if not isinstance(item, str):
            return None, "bad_ingredients"
        s = item.strip()
        if not (1 <= len(s) <= 200):
            return None, "bad_ingredients"
        clean_ing.append(s)

    steps = data.get("steps")
    if not isinstance(steps, list) or not (1 <= len(steps) <= 30):
        return None, "bad_steps"
    clean_steps = []
    for item in steps:
        if not isinstance(item, str):
            return None, "bad_steps"
        s = item.strip()
        if not (1 <= len(s) <= 500):
            return None, "bad_steps"
        clean_steps.append(s)

    return {"title": title, "ingredients": clean_ing, "steps": clean_steps}, None


# ═════════ AI adapter ═════════

def ai_generate(api_key, model, prompt_text):
    """
    Қайтарады: (text, None) немесе (None, error_key).
    error_key: 'timeout' | 'empty' | 'service'. Кілт сыртқа шықпайды.
    """
    try:
        client = genai.Client(api_key=api_key)
        interaction = client.interactions.create(model=model, input=prompt_text)
        text = (interaction.output_text or "").strip()
        if not text:
            return None, "empty"
        return text, None
    except Exception as exc:
        name = type(exc).__name__.lower()
        msg = str(exc).lower()
        if "timeout" in name or "timeout" in msg or "deadline" in msg:
            return None, "timeout"
        return None, "service"


# ═════════ Pending (серверде, session бойынша оқшауланған) ═════════

def _get_pending(request):
    pending = request.session.get("smartchef_pending")
    if not pending:
        return None
    if pending.get("expires_at", 0) < time.time():
        request.session.pop("smartchef_pending", None)
        return None
    return pending


def _public_pending(pending):
    if not pending:
        return None
    return {
        "pending_id": pending["pending_id"],
        "token": pending["token"],
        "candidate": pending["candidate"],
    }


def _set_pending(request, original_recipe, candidate):
    current_draft = request.session.get("smartchef_draft") or {}
    now = time.time()
    pending = {
        "pending_id": str(uuid.uuid4()),
        "token": secrets.token_urlsafe(32),
        "base_recipe_id": current_draft.get("token", ""),
        "original": original_recipe,
        "candidate": candidate,
        "created_at": now,
        "expires_at": now + PENDING_TTL_SECONDS,
    }
    request.session["smartchef_pending"] = pending
    return pending


def _format_candidate(candidate, language):
    if language == "kk":
        ing_label = "Ингредиенттер:"
        steps_label = "Дайындау қадамдары:"
    else:
        ing_label = "Ингредиенты:"
        steps_label = "Шаги приготовления:"
    lines = [candidate["title"], "", ing_label]
    for item in candidate["ingredients"]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append(steps_label)
    for i, step in enumerate(candidate["steps"], 1):
        lines.append(f"{i}. {step}")
    return "\n".join(lines)


def _msg(error_key, language):
    kk = {
        "timeout": "AI қызметі жауап бермеді (уақыт бітті). Қайталап көріңіз.",
        "empty": "AI бос жауап қайтарды. Қайталап көріңіз.",
        "service": "AI қызметінде қате болды. Кейінірек қайталап көріңіз.",
        "invalid_json": "AI жауабы JSON форматында емес.",
        "duplicate_keys": "AI жауабында қайталанған кілттер бар.",
        "wrong_keys": "AI жауабында артық не жетіспейтін өлшем бар.",
        "not_object": "AI жауабы объект емес.",
        "bad_title": "AI жауабында атауы дұрыс емес (3–200 таңба, бір жол).",
        "bad_ingredients": "AI жауабында ингредиенттер дұрыс емес (1–30, әрқайсысы 1–200).",
        "bad_steps": "AI жауабында қадамдар дұрыс емес (1–30, әрқайсысы 1–500).",
        "prompt": "Сұрау 3–300 таңба аралығында болуы керек.",
        "no_key": ".env файлына жарамды GEMINI_API_KEY кілтін енгізіңіз.",
    }
    ru = {
        "timeout": "Сервис AI не ответил (таймаут). Попробуйте ещё раз.",
        "empty": "AI вернул пустой ответ. Попробуйте ещё раз.",
        "service": "Ошибка сервиса AI. Попробуйте позже.",
        "invalid_json": "Ответ AI не в формате JSON.",
        "duplicate_keys": "В ответе AI повторяющиеся ключи.",
        "wrong_keys": "В ответе AI лишние или отсутствующие поля.",
        "not_object": "Ответ AI не является объектом.",
        "bad_title": "Некорректное название в ответе AI (3–200 символов, одна строка).",
        "bad_ingredients": "Некорректные ингредиенты в ответе AI (1–30, каждый 1–200).",
        "bad_steps": "Некорректные шаги в ответе AI (1–30, каждый 1–500).",
        "prompt": "Запрос должен быть от 3 до 300 символов.",
        "no_key": "Укажите действительный GEMINI_API_KEY в файле .env.",
    }
    return (kk if language == "kk" else ru).get(error_key, error_key)


def _build_suggest_prompt(base_recipe, prompt, language):
    if language == "kk":
        base_note = (
            f"Ағымдағы рецепт мәтіні:\n{base_recipe}\n\n" if base_recipe else "Ағымдағы рецепт жоқ.\n\n"
        )
        return f"""Сен SmartChef аспаз көмекшісісің. Пайдаланушының сұрауы бойынша рецепт жаса немесе бар рецептті жаңарт.

{base_note}Пайдаланушы сұрауы:
{prompt}

Жауапты ТЕК қатаң JSON форматында бер (markdown, түсініктеме, code fence жоқ):
{{"title": "...", "ingredients": ["...", ...], "steps": ["...", ...]}}

Шектеулер:
- title: 3–200 таңба, бір жолда
- ingredients: 1–30 элемент, әрқайсысы 1–200 таңба
- steps: 1–30 элемент, әрқайсысы 1–500 таңба
- Тек осы 3 кілт болсын. Артық немесе қайталанған кілт болмасын.
- Бос жол болмасын.""".strip()
    base_note = (
        f"Текущий текст рецепта:\n{base_recipe}\n\n" if base_recipe else "Текущего рецепта нет.\n\n"
    )
    return f"""Ты кулинарный помощник SmartChef. По запросу пользователя создай рецепт или обнови существующий.

{base_note}Запрос пользователя:
{prompt}

Ответ верни ТОЛЬКО в строгом JSON-формате (без markdown, без пояснений, без code fence):
{{"title": "...", "ingredients": ["...", ...], "steps": ["...", ...]}}

Ограничения:
- title: 3–200 символов, одна строка
- ingredients: 1–30 элементов, каждый 1–200 символов
- steps: 1–30 элементов, каждый 1–500 символов
- Только эти 3 ключа. Никаких лишних или повторяющихся ключей.
- Никаких пустых строк.""".strip()


# ═════════ index ═════════

def index(request):
    language = "kk"
    products = ""
    prompt = ""
    minutes = "30"
    vegetarian = False
    recipe = ""
    error = ""
    can_save = False
    compare_items = []
    selected_products = []
    history = SavedRecipe.objects.all()[:12]

    draft_session = request.session.get("smartchef_draft")
    if draft_session:
        recipe = draft_session.get("recipe", "")
        can_save = bool(recipe)

    def render_index():
        pending_session = _get_pending(request)
        return render(
            request,
            "recipes/index.html",
            {
                "language": language,
                "products": products,
                "prompt": prompt,
                "minutes": minutes,
                "vegetarian": vegetarian,
                "recipe": recipe,
                "error": error,
                "can_save": can_save,
                "can_undo": bool(request.session.get("smartchef_undo")),
                "history": history,
                "categories": CATEGORIES,
                "catalog_products": PRODUCTS,
                "selected_products": selected_products,
                "compare_items": compare_items,
                "pending": _public_pending(pending_session),
                "draft_token": (request.session.get("smartchef_draft") or {}).get("token", ""),
                "catalog_data": {
                    "categories": CATEGORIES,
                    "products": PRODUCTS,
                    "language": language,
                },
            },
        )

    if request.method == "GET":
        language = _safe_language(request.GET.get("language", "kk"))
        candidates = request.session.get("smartchef_candidates") or {}
        compare_items = candidates.get("items") or []
        return render_index()

    language = _safe_language(request.POST.get("language", "kk"))
    products = request.POST.get("products", "")
    prompt = request.POST.get("prompt", "").strip()
    minutes = request.POST.get("minutes", "30")
    vegetarian = request.POST.get("vegetarian") == "1"
    action = request.POST.get("action", "")
    raw_ids = request.POST.getlist("selected_products")

    lookup = {p["id"] for p in PRODUCTS}
    selected_products = [pid for pid in dict.fromkeys(raw_ids) if pid in lookup]

    if action == "language":
        request.session.pop("smartchef_draft", None)
        request.session.pop("smartchef_candidates", None)
        request.session.pop("smartchef_pending", None)
        recipe = ""
        error = ""
        can_save = False
        return render_index()

    # ── Редактор: сұрау арқылы ұсыныс алу ──
    if action == "suggest":
        if not (3 <= len(prompt) <= 300):
            error = _msg("prompt", language)
            return render_index()

        base_recipe_id = request.POST.get("base_recipe_id", "").strip()
        base_recipe = ""
        draft = request.session.get("smartchef_draft")
        if draft and base_recipe_id and draft.get("token") == base_recipe_id:
            base_recipe = draft.get("recipe", "")

        config = dotenv_values(settings.BASE_DIR / ".env")
        api_key = (config.get("GEMINI_API_KEY") or "").strip()
        model = (config.get("GEMINI_MODEL") or "").strip() or "gemini-3.8-flash"
        if not api_key or api_key == "PASTE_YOUR_KEY_HERE":
            error = _msg("no_key", language)
            return render_index()

        ai_prompt = _build_suggest_prompt(base_recipe, prompt, language)
        text, err = ai_generate(api_key, model, ai_prompt)
        if err:
            error = _msg(err, language)
            return render_index()

        candidate, verr = validate_recipe_payload(text)
        if verr:
            error = _msg(verr, language)
            return render_index()

        _set_pending(request, base_recipe, candidate)
        return render_index()

    if action not in ("generate", "compare"):
        error = (
            "Белгісіз әрекет. Қайтадан байқап көріңіз."
            if language == "kk"
            else "Неизвестное действие. Попробуйте ещё раз."
        )
        return render_index()

    request.session.pop("smartchef_draft", None)
    request.session.pop("smartchef_candidates", None)
    request.session.pop("smartchef_pending", None)
    recipe = ""
    can_save = False

    try:
        selected_products, combined = build_products(raw_ids, products, language)
    except ValueError as exc:
        error = str(exc)
        return render_index()

    if minutes not in ("15", "30", "60"):
        error = (
            "Дайындау уақыты қате. 15, 30 немесе 60 минутты таңдаңыз."
            if language == "kk"
            else "Некорректное время приготовления. Выберите 15, 30 или 60 минут."
        )
        return render_index()

    config = dotenv_values(settings.BASE_DIR / ".env")
    api_key = (config.get("GEMINI_API_KEY") or "").strip()
    model = (config.get("GEMINI_MODEL") or "").strip() or "gemini-3.8-flash"
    if not api_key or api_key == "PASTE_YOUR_KEY_HERE":
        error = _msg("no_key", language)
        return render_index()

    recent_memory = list(SavedRecipe.objects.order_by("-created_at")[:6])
    recent_memory.reverse()
    if recent_memory:
        memory_context = "\n\n".join(
            f"Previous request: {item.products}\nPrevious answer: {item.recipe[:1800]}"
            for item in recent_memory
        )
    else:
        memory_context = "No previous requests yet."

    if language == "kk":
        output_language = "қазақ тілінде"
        vegetarian_condition = (
            "Рецепт міндетті түрде вегетариандық болуы керек. Ет, құс еті және балық қолданба."
            if vegetarian
            else "Вегетариандық шектеу жоқ."
        )
        prompt_text = f"""
Сен SmartChef оқу қолданбасының аспаз көмекшісісің. Алдыңғы сұраныстарды контекст ретінде ескер, бірақ қазіргі сұранысқа басымдық бер.

Алдыңғы сұраныстар мен жауаптар:
{memory_context}

Қолданушыдағы өнімдер:
{combined}

Шарттар:
- Дайындау уақыты: ең көбі {minutes} минут.
- {vegetarian_condition}
- Жауапты тек {output_language} жаз.
- Берілген өнімдерге барынша сүйен.
- Қарапайым және түсінікті рецепт жаса.
- Дәл бір ғана рецепт ұсын.
- Жауапта артық кіріспе немесе қорытынды жазба.

Жауаптың құрылымы дәл мына бөлімдерден тұрсын:

Рецепт атауы:
...

Дайындау уақыты:
...

Ингредиенттер:
- ...

Дайындау қадамдары:
1. ...
2. ...
3. ...

Кеңес:
...
""".strip()
        compare_suffix = "\n\nНұсқа {n}."
        compare_fail = "Салыстыру үшін екі рецепт жасау мүмкін болмады."
    else:
        output_language = "на русском языке"
        vegetarian_condition = (
            "Рецепт обязательно должен быть вегетарианским. Не используй мясо, птицу и рыбу."
            if vegetarian
            else "Ограничений по вегетарианскому питанию нет."
        )
        prompt_text = f"""
Ты кулинарный помощник учебного приложения SmartChef. Учитывай прошлые запросы как контекст и предпочтения, но текущий запрос всегда важнее.

Предыдущие запросы и ответы:
{memory_context}

Продукты пользователя:
{combined}

Условия:
- Время приготовления: не более {minutes} минут.
- {vegetarian_condition}
- Ответ напиши только {output_language}.
- Максимально используй указанные пользователем продукты.
- Рецепт должен быть простым и понятным.
- Предложи ровно один рецепт.
- Не добавляй лишнее вступление или заключение.

Структура ответа должна содержать следующие разделы:

Название рецепта:
...

Время приготовления:
...

Ингредиенты:
- ...

Шаги приготовления:
1. ...
2. ...
3. ...

Совет:
...
""".strip()
        compare_suffix = "\n\nВариант {n}."
        compare_fail = "Не удалось создать два рецепта для сравнения."

    try:
        client = genai.Client(api_key=api_key)

        if action == "compare":
            results = []
            for n in (1, 2):
                interaction = client.interactions.create(
                    model=model, input=prompt_text + compare_suffix.format(n=n)
                )
                text = (interaction.output_text or "").strip()
                if not text:
                    continue
                results.append(
                    {
                        "token": str(uuid.uuid4()),
                        "products": combined,
                        "recipe": text,
                        "language": language,
                        "minutes": int(minutes),
                        "vegetarian": bool(vegetarian),
                    }
                )
            if len(results) == 2:
                request.session["smartchef_candidates"] = {"items": results}
                compare_items = results
            else:
                error = compare_fail
        else:
            interaction = client.interactions.create(model=model, input=prompt_text)
            recipe = (interaction.output_text or "").strip()
            if recipe:
                request.session["smartchef_draft"] = {
                    "token": str(uuid.uuid4()),
                    "products": combined,
                    "recipe": recipe,
                    "language": language,
                    "minutes": int(minutes),
                    "vegetarian": bool(vegetarian),
                }
                can_save = True
            else:
                error = _msg("empty", language)
                recipe = ""
    except Exception as exc:
        name = type(exc).__name__.lower()
        msg = str(exc).lower()
        if "timeout" in name or "timeout" in msg or "deadline" in msg:
            error = _msg("timeout", language)
        else:
            error = _msg("service", language)
        recipe = ""

    return render_index()


# ═════════ Pending accept/reject ═════════
#
# POST + CSRF (`@require_POST` + шаблонда `{% csrf_token %}`).
# Accept: server-side pending_id + token тексеріледі, expiry `_get_pending`-те.
# Client жіберген recipe мәтіні ЕШҚАШАН сақталмайды — candidate-тен ғана құрастырылады.
# Conditional update: current draft.recipe == pending.original болса ғана.
# Транзакция: draft + undo бір transaction ішінде.
# Atomicity кепілі: тек DB-backed session (django.contrib.sessions.backends.db,
#   осы жобада әдепкі). Cache/cookie session-да атомарлық ЖОҚ.

@require_POST
def accept_pending(request):
    pending = _get_pending(request)
    pid = request.POST.get("pending_id", "")
    tok = request.POST.get("token", "")
    language = _safe_language(request.POST.get("language", "kk"))

    if not pending or pending.get("pending_id") != pid or pending.get("token") != tok:
        return redirect(f"/?language={language}")

    current_draft = request.session.get("smartchef_draft") or {}
    current_recipe = current_draft.get("recipe", "")
    if current_recipe != pending.get("original", ""):
        # Stale proposal — overwrite жасалмайды
        return redirect(f"/?language={language}")

    new_text = _format_candidate(pending["candidate"], language)
    old_snapshot = dict(current_draft)  # accept алдындағы толық recipe snapshot

    if current_draft:
        new_draft = dict(current_draft)
    else:
        new_draft = {
            "token": str(uuid.uuid4()),
            "products": "",
            "recipe": "",
            "language": language,
            "minutes": 30,
            "vegetarian": False,
        }
    new_draft["recipe"] = new_text
    new_draft["language"] = language

    with transaction.atomic():
        request.session["smartchef_draft"] = new_draft
        request.session["smartchef_undo"] = {
            "action": "accept_pending",
            "recipe_id": new_draft["token"],
            "old_draft": old_snapshot,
            "after": new_text,
        }
        request.session.pop("smartchef_pending", None)
        request.session.save()

    return redirect(f"/?language={language}")


@require_POST
def reject_pending(request):
    pending = _get_pending(request)
    pid = request.POST.get("pending_id", "")
    tok = request.POST.get("token", "")
    language = _safe_language(request.POST.get("language", "kk"))

    if not pending or pending.get("pending_id") != pid or pending.get("token") != tok:
        return redirect(f"/?language={language}")

    # Reject тек pending-ті өшіреді. Draft/SavedRecipe-ке тиіспейді.
    with transaction.atomic():
        request.session.pop("smartchef_pending", None)
        request.session["smartchef_undo"] = {"action": "reject_pending", "pending": pending}
        request.session.save()

    return redirect(f"/?language={language}")


# ═════════ Бар save/reject/accept_candidate/reject_candidate/undo/saved ═════════

def _save_draft_to_db(draft):
    # Owner сүзгісі жоқ (SavedRecipe моделінде owner өрісі жоқ).
    # Болашақта owner қосылса, осында owner=request.user қосылады.
    SavedRecipe.objects.get_or_create(
        token=draft["token"],
        defaults={
            "products": draft["products"],
            "recipe": draft["recipe"],
            "language": draft["language"],
            "minutes": draft["minutes"],
            "vegetarian": draft["vegetarian"],
        },
    )


@require_POST
def save_recipe(request):
    draft = request.session.get("smartchef_draft")
    if not draft:
        return redirect("recipes:index")

    _save_draft_to_db(draft)
    request.session["smartchef_undo"] = {
        "action": "save",
        "token": draft["token"],
        "draft": dict(draft),
    }
    request.session.pop("smartchef_draft", None)

    language = _safe_language(draft.get("language", "kk"))
    return redirect(f"/saved/?language={language}")


@require_POST
def accept_candidate(request):
    token = request.POST.get("token", "")
    candidates = request.session.get("smartchef_candidates") or {}
    items = candidates.get("items") or []
    item = next((i for i in items if i.get("token") == token), None)
    if not item:
        return redirect("recipes:index")

    _save_draft_to_db(item)
    request.session["smartchef_undo"] = {
        "action": "save",
        "token": item["token"],
        "draft": dict(item),
    }

    remaining = [i for i in items if i.get("token") != token]
    if remaining:
        request.session["smartchef_candidates"] = {"items": remaining}
    else:
        request.session.pop("smartchef_candidates", None)

    language = _safe_language(item.get("language", "kk"))
    return redirect(f"/saved/?language={language}")


@require_POST
def reject_recipe(request):
    draft = request.session.pop("smartchef_draft", None)
    language = _safe_language((draft or {}).get("language") or request.POST.get("language", "kk"))
    if draft:
        request.session["smartchef_undo"] = {"action": "reject", "draft": dict(draft)}
    return redirect(f"/?language={language}")


@require_POST
def reject_candidate(request):
    token = request.POST.get("token", "")
    language = _safe_language(request.POST.get("language", "kk"))
    candidates = request.session.get("smartchef_candidates") or {}
    items = candidates.get("items") or []
    rejected = next((i for i in items if i.get("token") == token), None)
    if not rejected:
        return redirect(f"/?language={language}")

    remaining = [i for i in items if i.get("token") != token]
    if remaining:
        request.session["smartchef_candidates"] = {"items": remaining}
    else:
        request.session.pop("smartchef_candidates", None)

    request.session["smartchef_undo"] = {"action": "reject_candidate", "draft": dict(rejected)}
    return redirect(f"/?language={language}")


@require_POST
def undo_last(request):
    """
    Бір деңгей undo.
    - `accept_pending` үшін: recipe ID + `undo.after` тексеріледі;
      `current recipe == undo.after` болмаса overwrite жасалмайды.
    - Басқа action-дар (save/reject/reject_candidate/reject_pending) — safe әрі atomic.
    - Екінші undo: `smartchef_undo` pop → `if not undo: redirect`.
    """
    language = _safe_language(request.POST.get("language", "kk"))
    undo = request.session.get("smartchef_undo")
    if not undo:
        return redirect(f"/?language={language}")

    action = undo.get("action")

    if action == "accept_pending":
        current_draft = request.session.get("smartchef_draft") or {}
        # recipe ID (draft token) сәйкес пе?
        if current_draft.get("token") != undo.get("recipe_id"):
            request.session.pop("smartchef_undo", None)
            request.session.save()
            return redirect(f"/?language={language}")
        # ағымдағы recipe == undo.after пе?
        if current_draft.get("recipe", "") != undo.get("after", ""):
            request.session.pop("smartchef_undo", None)
            request.session.save()
            return redirect(f"/?language={language}")

        with transaction.atomic():
            old_draft = undo.get("old_draft")
            if old_draft:
                request.session["smartchef_draft"] = dict(old_draft)
            else:
                request.session.pop("smartchef_draft", None)
            request.session.pop("smartchef_undo", None)
            request.session.pop("smartchef_pending", None)
            request.session.save()

        return redirect(f"/?language={language}")

    # Басқа action-дар
    with transaction.atomic():
        if action == "save":
            token = undo.get("token")
            if token:
                SavedRecipe.objects.filter(token=token).delete()
            draft = undo.get("draft")
            if draft:
                request.session["smartchef_draft"] = dict(draft)
        elif action == "reject":
            draft = undo.get("draft")
            if draft:
                request.session["smartchef_draft"] = dict(draft)
        elif action == "reject_candidate":
            draft = undo.get("draft")
            if draft:
                items = (request.session.get("smartchef_candidates") or {}).get("items", [])
                items.append(dict(draft))
                request.session["smartchef_candidates"] = {"items": items}
        elif action == "reject_pending":
            pass
        request.session.pop("smartchef_undo", None)
        request.session.save()

    return redirect(f"/?language={language}")


def saved_recipes(request):
    language = _safe_language(request.GET.get("language", "kk"))
    items = SavedRecipe.objects.all()
    return render(
        request,
        "recipes/saved.html",
        {"saved_recipes": items, "language": language},
    )