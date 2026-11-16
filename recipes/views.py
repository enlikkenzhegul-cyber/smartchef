import uuid

from django.conf import settings
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST
from dotenv import dotenv_values
from google import genai

from .models import SavedRecipe
from .catalog import CATEGORIES, PRODUCTS, build_products


def index(request):
    language = "kk"
    products = ""
    minutes = "30"
    vegetarian = False
    recipe = ""
    error = ""
    can_save = False
    history = SavedRecipe.objects.all()[:12]
    selected_products = []

    def render_index():
        return render(
            request,
            "recipes/index.html",
            {
                "language": language,
                "products": products,
                "minutes": minutes,
                "vegetarian": vegetarian,
                "recipe": recipe,
                "error": error,
                "can_save": can_save,
                "history": history,
                "categories": CATEGORIES,
                "catalog_products": PRODUCTS,
                "selected_products": selected_products,
                "catalog_data": {
                    "categories": CATEGORIES,
                    "products": PRODUCTS,
                    "language": language,
                },
            },
        )

    if request.method == "GET":
        language = request.GET.get("language", "kk")
        if language not in ("kk", "ru"):
            language = "kk"
        return render_index()

    language = request.POST.get("language", "kk")
    if language not in ("kk", "ru"):
        language = "kk"

    products = request.POST.get("products", "")
    minutes = request.POST.get("minutes", "30")
    vegetarian = request.POST.get("vegetarian") == "1"
    action = request.POST.get("action", "")
    raw_ids = request.POST.getlist("selected_products")

    lookup = {p["id"] for p in PRODUCTS}
    selected_products = [pid for pid in dict.fromkeys(raw_ids) if pid in lookup]

    if action == "language":
        request.session.pop("smartchef_draft", None)
        recipe = ""
        error = ""
        can_save = False
        return render_index()

    if action != "generate":
        error = (
            "Белгісіз әрекет. Қайтадан байқап көріңіз."
            if language == "kk"
            else "Неизвестное действие. Попробуйте ещё раз."
        )
        return render_index()

    request.session.pop("smartchef_draft", None)
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
        error = (
            ".env файлына жарамды GEMINI_API_KEY кілтін енгізіңіз."
            if language == "kk"
            else "Укажите действительный GEMINI_API_KEY в файле .env."
        )
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
            "Рецепт міндетті түрде вегетариандық болуы керек. "
            "Ет, құс еті және балық қолданба."
            if vegetarian
            else "Вегетариандық шектеу жоқ."
        )

        prompt = f"""
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

    else:
        output_language = "на русском языке"
        vegetarian_condition = (
            "Рецепт обязательно должен быть вегетарианским. "
            "Не используй мясо, птицу и рыбу."
            if vegetarian
            else "Ограничений по вегетарианскому питанию нет."
        )

        prompt = f"""
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

    try:
        client = genai.Client(api_key=api_key)
        interaction = client.interactions.create(
            model=model,
            input=prompt,
        )
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
            error = (
                "Рецепт жасау мүмкін болмады. Кейінірек қайтадан байқап көріңіз."
                if language == "kk"
                else "Не удалось создать рецепт. Попробуйте ещё раз позже."
            )
            recipe = ""

    except Exception:
        error = (
            "Рецепт жасау кезінде сервис қатесі пайда болды. Кейінірек қайтадан байқап көріңіз."
            if language == "kk"
            else "Во время создания рецепта произошла ошибка сервиса. Попробуйте ещё раз позже."
        )
        recipe = ""

    return render_index()


@require_POST
def save_recipe(request):
    draft = request.session.get("smartchef_draft")

    if not draft:
        return redirect("recipes:index")

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

    language = draft.get("language", "kk")
    if language not in ("kk", "ru"):
        language = "kk"

    return redirect(f"/saved/?language={language}")


def saved_recipes(request):
    language = request.GET.get("language", "kk")

    if language not in ("kk", "ru"):
        language = "kk"

    saved_recipes = SavedRecipe.objects.all()

    return render(
        request,
        "recipes/saved.html",
        {
            "saved_recipes": saved_recipes,
            "language": language,
        },
    )