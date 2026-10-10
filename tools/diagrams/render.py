"""Рендерит диаграммы README в SVG, по одному файлу на цветовую схему.

Нужны два файла, потому что GitHub выбирает между ними через <picture> и
prefers-color-scheme; ручная поддержка обоих гарантировала бы расхождение.
Поэтому геометрия и содержимое описаны один раз, а меняется только палитра.

Фоны — это собственные цвета холста GitHub, поэтому диаграмма стоит на странице
без заметного края карточки вокруг неё — рисунок словно парит на README, а не
сидит в рамке, которая не совсем совпадает.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace"


@dataclass(frozen=True, slots=True)
class Palette:
    name: str
    bg: str
    surface: str
    surface_alt: str
    border: str
    text: str
    muted: str
    accent: str
    accent_soft: str
    success: str
    success_soft: str
    danger: str
    danger_soft: str
    attention: str
    attention_soft: str


#: Светлая и тёмная палитры холста GitHub (Primer).
LIGHT = Palette(
    name="light",
    bg="#ffffff",
    surface="#f6f8fa",
    surface_alt="#eaeef2",
    border="#d1d9e0",
    text="#1f2328",
    muted="#59636e",
    accent="#0969da",
    accent_soft="#ddf4ff",
    success="#1a7f37",
    success_soft="#dafbe1",
    danger="#cf222e",
    danger_soft="#ffebe9",
    attention="#9a6700",
    attention_soft="#fff8c5",
)

DARK = Palette(
    name="dark",
    bg="#0d1117",
    surface="#151b23",
    surface_alt="#212830",
    border="#3d444d",
    text="#e6edf3",
    muted="#9198a1",
    accent="#4493f8",
    accent_soft="#121d2f",
    success="#3fb950",
    success_soft="#0f2913",
    danger="#f85149",
    danger_soft="#2b1618",
    attention="#d29922",
    attention_soft="#2b2412",
)

TONES = {
    "neutral": lambda p: (p.surface, p.border, p.text),
    "accent": lambda p: (p.accent_soft, p.accent, p.text),
    "success": lambda p: (p.success_soft, p.success, p.text),
    "danger": lambda p: (p.danger_soft, p.danger, p.text),
    "attention": lambda p: (p.attention_soft, p.attention, p.text),
    "ghost": lambda p: (p.bg, p.border, p.muted),
}


#: Приблизительная ширина символа как доля размера шрифта. В SVG нет механизма
#: движка, поэтому вместимость подписи можно определить только оценочно.
#: Кириллица и латиница в нижнем регистре при этих размерах достаточно близки;
#: один коэффициент подходит обоим и слегка завышает ширину для надёжности.
CHAR_WIDTH = 0.55
WIDE_CHARS = set("MWmwФШЩЫЮЖ")
BODY_SIZE = 12.5
TITLE_SIZE = 14
SMALL_SIZE = 12


def text_width(text: str, size: float) -> float:
    total = 0.0
    for char in text:
        if char == " ":
            total += 0.28
        elif char in WIDE_CHARS:
            total += 0.78
        elif char.isupper():
            total += 0.64
        else:
            total += CHAR_WIDTH
    return total * size


def wrap(text: str, max_width: float, size: float) -> list[str]:
    """Разбивает подпись на строки, которые помещаются. Явные переносы сохраняются.

    Написано потому, что после перевода подписей несколько из них вышли за
    пределы блоков, а ручная подгонка каждой лишь отложила бы проблему до
    следующей правки.
    """
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split(" "):
            candidate = f"{current} {word}".strip()
            if current and text_width(candidate, size) > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    return lines


# ── Примитивы ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Canvas:
    width: int
    height: int
    parts: list[str] = field(default_factory=list)

    def add(self, markup: str) -> None:
        self.parts.append(markup)

    def render(self, palette: Palette, *, title: str, description: str) -> str:
        body = "\n".join(self.parts)
        return f"""<svg xmlns="http://www.w3.org/2000/svg" \
viewBox="0 0 {self.width} {self.height}" width="{self.width}" height="{self.height}" \
role="img" aria-labelledby="title desc" font-family="{FONT}">
  <title id="title">{escape(title)}</title>
  <desc id="desc">{escape(description)}</desc>
  <defs>
    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" \
markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="{palette.muted}"/>
    </marker>
    <marker id="arrow-accent" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" \
markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="{palette.accent}"/>
    </marker>
    <marker id="arrow-danger" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" \
markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="{palette.danger}"/>
    </marker>
  </defs>
  <rect width="{self.width}" height="{self.height}" fill="{palette.bg}"/>
{body}
</svg>
"""


def box(
    p: Palette,
    x: float,
    y: float,
    w: float,
    h: float,
    title: str,
    subtitle: str = "",
    *,
    tone: str = "neutral",
    dashed: bool = False,
    mono: bool = False,
    radius: int = 8,
    title_size: float = TITLE_SIZE,
    subtitle_size: float = BODY_SIZE,
) -> str:
    fill, stroke, text = TONES[tone](p)
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    lines = [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"{dash}/>'
    ]
    cx = x + w / 2
    if subtitle:
        wrapped = wrap(subtitle, w - 24, subtitle_size)
        line_height = subtitle_size + 2
        content_height = title_size + 3 + len(wrapped) * line_height
        content_top = y + (h - content_height) / 2
        title_y = content_top + title_size * 0.82
        lines.append(
            f'<text x="{cx}" y="{title_y}" text-anchor="middle" font-size="{title_size}" '
            f'font-weight="600" fill="{text}"'
            + (f' font-family="{MONO}"' if mono else "")
            + f">{escape(title)}</text>"
        )
        for index, part in enumerate(wrapped):
            lines.append(
                f'<text x="{cx}" y="{title_y + 3 + line_height * (index + 1)}" '
                f'text-anchor="middle" font-size="{subtitle_size}" '
                f'fill="{p.muted}">{escape(part)}</text>'
            )
    else:
        lines.append(
            f'<text x="{cx}" y="{y + h / 2 + title_size * 0.36}" text-anchor="middle" '
            f'font-size="{title_size}" '
            f'font-weight="600" fill="{text}"'
            + (f' font-family="{MONO}"' if mono else "")
            + f">{escape(title)}</text>"
        )
    return "\n".join(lines)


def region(
    p: Palette,
    x: float,
    y: float,
    w: float,
    h: float,
    label: str,
    *,
    tone: str = "ghost",
    dashed: bool = True,
) -> str:
    _, stroke, _ = TONES[tone](p)
    dash = ' stroke-dasharray="6 5"' if dashed else ""
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="none" '
        f'stroke="{stroke}" stroke-width="1.25"{dash}/>\n'
        f'<text x="{x + w / 2}" y="{y + 20}" text-anchor="middle" '
        f'font-size="{SMALL_SIZE}" font-weight="600" letter-spacing="0.6" '
        f'fill="{p.muted}">{escape(label.upper())}</text>'
    )


def arrow(
    p: Palette,
    points: list[tuple[float, float]],
    label: str = "",
    *,
    tone: str = "muted",
    dashed: bool = False,
    label_dx: float = 0,
    label_dy: float = -7,
) -> str:
    colour = {"muted": p.muted, "accent": p.accent, "danger": p.danger}[tone]
    marker = {"muted": "arrow", "accent": "arrow-accent", "danger": "arrow-danger"}[tone]
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    path = " ".join(
        ("M" if index == 0 else "L") + f" {x} {y}" for index, (x, y) in enumerate(points)
    )
    markup = (
        f'<path d="{path}" fill="none" stroke="{colour}" stroke-width="1.6"{dash} '
        f'marker-end="url(#{marker})"/>'
    )
    if label:
        mid = points[len(points) // 2]
        start = points[len(points) // 2 - 1]
        mx, my = (mid[0] + start[0]) / 2, (mid[1] + start[1]) / 2
        markup += (
            f'\n<text x="{mx + label_dx}" y="{my + label_dy}" text-anchor="middle" '
            f'font-size="{SMALL_SIZE}" fill="{p.muted}">{escape(label)}</text>'
        )
    return markup


def caption(
    p: Palette,
    x: float,
    y: float,
    text: str,
    *,
    anchor: str = "start",
    size: float = BODY_SIZE,
    muted: bool = True,
    mono: bool = False,
) -> str:
    family = f' font-family="{MONO}"' if mono else ""
    return (
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" '
        f'fill="{p.muted if muted else p.text}"{family}>{escape(text)}</text>'
    )


def heading(p: Palette, x: float, y: float, text: str) -> str:
    return (
        f'<text x="{x}" y="{y}" font-size="13.5" font-weight="700" letter-spacing="0.7" '
        f'fill="{p.muted}">{escape(text.upper())}</text>'
    )


# ── Диаграмма 1: архитектура ────────────────────────────────────────────────


def architecture(p: Palette) -> Canvas:
    c = Canvas(1000, 660)

    c.add(heading(p, 32, 34, "AI Operations Agent — архитектура системы"))
    c.add(
        caption(
            p, 32, 54, "Всё, до чего агент дотягивается, и что стоит между ним и каждой системой."
        )
    )

    c.add(box(p, 32, 78, 190, 62, "Инженер", "задача на естественном языке", tone="accent"))
    c.add(arrow(p, [(127, 140), (127, 168)]))

    # Слой API
    c.add(region(p, 32, 168, 390, 120, "FastAPI"))
    c.add(box(p, 48, 196, 172, 34, "POST /runs", mono=True, radius=6, title_size=13.5))
    c.add(
        box(
            p,
            48,
            238,
            172,
            34,
            "POST /approval",
            mono=True,
            radius=6,
            tone="attention",
            title_size=13.5,
        )
    )
    c.add(
        box(
            p,
            234,
            196,
            172,
            34,
            "GET /runs/{id}/trace",
            mono=True,
            radius=6,
            title_size=13.5,
        )
    )
    c.add(box(p, 234, 238, 172, 34, "GET /metrics", mono=True, radius=6, title_size=13.5))

    # Ядро агента
    c.add(region(p, 32, 308, 390, 184, "воркфлоу LangGraph"))
    c.add(box(p, 48, 338, 172, 62, "Состояние", "типизированное, в чекпоинте", tone="neutral"))
    c.add(box(p, 234, 338, 172, 62, "Ограничения", "бюджеты · allowlist · чтение/запись"))
    c.add(
        box(
            p,
            48,
            412,
            172,
            76,
            "Планировщик",
            "выбирает следующий инструмент\nиз разрешённых",
            tone="accent",
        )
    )
    c.add(
        box(
            p,
            234,
            412,
            172,
            76,
            "Подтверждение",
            "пауза перед любой\nзаписью человеком",
            tone="attention",
        )
    )

    # LLM
    c.add(
        box(
            p,
            32,
            516,
            390,
            64,
            "LLM  ·  LangChain",
            "необязателен: без него агент идёт детерминированным путём",
            tone="ghost",
            dashed=True,
        )
    )
    c.add(arrow(p, [(227, 492), (227, 516)], dashed=True))

    c.add(arrow(p, [(227, 288), (227, 308)], tone="accent"))

    # Граница MCP
    c.add(arrow(p, [(422, 392), (486, 392)], "MCP", tone="accent"))
    c.add(region(p, 486, 168, 482, 322, "слой интеграции MCP"))
    c.add(
        box(
            p,
            504,
            196,
            446,
            66,
            "Пул MCP-клиентов",
            "находит инструменты · чтение/запись по аннотациям · деградирует по серверам",
            tone="accent",
        )
    )

    servers = [
        ("monitoring", "метрики · алерты\nагрегированные ошибки", "success"),
        ("code", "деплои\nкоммиты · PR", "danger"),
        ("incident", "задачи\nсоздание · комментарии", "danger"),
        ("knowledge", "рунбуки\nпоиск", "success"),
    ]
    for index, (name, detail, tone) in enumerate(servers):
        x = 504 + index * 113
        c.add(
            box(
                p,
                x,
                280,
                108,
                94,
                name,
                detail,
                tone=tone,
                mono=True,
                title_size=12.5,
                subtitle_size=12,
            )
        )
        c.add(arrow(p, [(x + 51, 262), (x + 51, 280)]))
        c.add(box(p, x, 398, 103, 50, "внешняя", "система", tone="ghost", dashed=True))
        c.add(arrow(p, [(x + 51, 374), (x + 51, 398)], dashed=True))

    c.add(
        caption(
            p,
            727,
            474,
            "каждый сервер — отдельный процесс, говорит по MCP через stdio",
            anchor="middle",
        )
    )

    # Хранилище и наблюдаемость
    c.add(region(p, 486, 510, 482, 126, "состояние и телеметрия"))
    c.add(
        box(
            p,
            504,
            540,
            154,
            76,
            "SQLite / PostgreSQL",
            "запуски · подтверждения\nаудит · чекпоинты",
        )
    )
    c.add(box(p, 670, 540, 140, 76, "Prometheus", "стоимость запуска\nбезопасность записи"))
    c.add(box(p, 822, 540, 128, 76, "Grafana", "дашборд\nи алерты"))
    c.add(arrow(p, [(422, 254), (486, 254)], "", tone="muted"))
    c.add(arrow(p, [(422, 548), (486, 570)]))

    return c


# ── Диаграмма 2: граф воркфлоу ──────────────────────────────────────────────


def workflow(p: Palette) -> Canvas:
    c = Canvas(1000, 738)

    c.add(heading(p, 32, 34, "Граф расследования"))
    c.add(
        caption(
            p,
            32,
            54,
            "Сначала детерминированная работа, затем ограниченный агентный цикл, затем человек.",
        )
    )

    w, h = 224, 58
    main = 300  # левая колонка: расследование
    right = 676  # правая колонка: вывод и действие
    mid_l, mid_r = main + w / 2, right + w / 2

    def node(x, y, title, subtitle="", tone="neutral"):
        c.add(box(p, x, y, w, h, title, subtitle, tone=tone, mono=True))

    # ── Левая колонка ───────────────────────────────────────────────────────
    c.add(box(p, mid_l - 38, 82, 76, 28, "START", tone="ghost", radius=14))
    c.add(arrow(p, [(mid_l, 110), (mid_l, 132)]))

    node(main, 132, "analyze_task", "сервис и временное окно")
    c.add(arrow(p, [(mid_l, 190), (mid_l, 206)]))

    node(main, 206, "collect_initial_context", "метрики · деплои · ошибки · алерты")
    c.add(arrow(p, [(main - 4, 235), (main - 76, 235)], tone="danger"))
    c.add(caption(p, main - 40, 222, "нет сигнала", anchor="middle", size=SMALL_SIZE))
    c.add(
        box(
            p,
            24,
            206,
            200,
            58,
            "insufficient_context",
            "останавливается и объясняет",
            tone="danger",
            mono=True,
        )
    )
    c.add(arrow(p, [(mid_l, 264), (mid_l, 280)]))

    node(main, 280, "correlate", "всплеск ↔ деплой ↔ коммит", tone="success")
    c.add(arrow(p, [(mid_l, 338), (mid_l, 382)]))

    # ── Цикл ────────────────────────────────────────────────────────────────
    c.add(region(p, main - 116, 352, w + 148, 264, "агентный цикл — ограниченный"))
    node(main, 382, "select_tool", "единственный реальный выбор модели", tone="accent")
    c.add(arrow(p, [(mid_l, 440), (mid_l, 458)]))
    node(main, 458, "execute_tool", "проверен · по таймауту · записан")
    c.add(arrow(p, [(mid_l, 516), (mid_l, 534)]))
    node(main, 534, "evaluate_observation", "это что-то изменило?")

    c.add(
        arrow(
            p,
            [(main, 563), (main - 92, 563), (main - 92, 411), (main, 411)],
            "есть что узнать",
            tone="accent",
            label_dx=-52,
            label_dy=4,
        )
    )
    c.add(
        caption(
            p,
            main - 104,
            604,
            "выход: без запроса · бюджет · нет прогресса · 4 итерации",
            size=SMALL_SIZE,
        )
    )

    # ── Правая колонка, читается снизу вверх ────────────────────────────────
    c.add(arrow(p, [(main + w, 563), (right + 14, 563)], "достаточно", label_dy=-9))
    node(right, 534, "generate_analysis", "структурирован · заземлён", tone="success")
    c.add(arrow(p, [(mid_r, 534), (mid_r, 512)]))

    node(right, 454, "propose_action", "уверенность ≥ 0.6, иначе ничего")
    c.add(arrow(p, [(mid_r, 454), (mid_r, 432)], tone="danger"))
    c.add(caption(p, mid_r + 10, 443, "предложена запись", size=SMALL_SIZE))
    c.add(
        arrow(
            p,
            [(right, 479), (right - 40, 479), (right - 40, 219), (right, 219)],
            "",
            tone="muted",
        )
    )
    c.add(caption(p, right - 50, 300, "писать нечего", anchor="end", size=SMALL_SIZE))

    node(right, 374, "request_approval", "пауза · состояние в чекпоинте", tone="attention")
    c.add(arrow(p, [(mid_r, 374), (mid_r, 352)], tone="danger"))
    c.add(caption(p, mid_r + 10, 363, "подтверждено", size=SMALL_SIZE))
    c.add(
        arrow(
            p,
            [(right + w, 399), (right + w + 40, 399), (right + w + 40, 219), (right + w, 219)],
            "",
        )
    )
    c.add(caption(p, right + w + 34, 300, "отклонено", anchor="end", size=SMALL_SIZE))

    node(right, 294, "execute_action", "один инструмент · один шаг", tone="danger")
    c.add(arrow(p, [(mid_r, 294), (mid_r, 248)]))

    node(right, 190, "final_response", "что сделал и чего не сделал")
    c.add(arrow(p, [(mid_r, 190), (mid_r, 164)]))
    c.add(box(p, mid_r - 32, 136, 64, 28, "END", tone="ghost", radius=14))

    # ── Подробное описание барьера ─────────────────────────────────────────
    c.add(box(p, 32, 634, 936, 72, "", "", tone="attention", radius=10))
    c.add(
        caption(
            p,
            52,
            660,
            "Пауза долговечная: состояние лежит в чекпоинте, поэтому решение приходит "
            "отдельным HTTP-запросом от отдельного человека —",
            muted=False,
            size=12.5,
        )
    )
    c.add(
        caption(
            p,
            52,
            683,
            "а выполняется то действие, которое сохранил граф, а не то, что несёт "
            "подтверждающий запрос.",
            size=12.5,
        )
    )
    return c


# ── Диаграмма 3: что останавливает агента ───────────────────────────────────


def guardrails(p: Palette) -> Canvas:
    c = Canvas(1000, 510)

    c.add(heading(p, 32, 34, "Чего агент не может"))
    c.add(
        caption(
            p,
            32,
            54,
            "Каждое ограничение ниже вшито в код и проверяется до запуска инструмента. "
            "Ничего из этого не является инструкцией в промпте.",
        )
    )

    lanes = [
        (
            "Модель предлагает",
            "accent",
            [
                "видит только разрешённые политикой инструменты",
                "отвечает именем и аргументами",
                "не может добавить инструмент или расширить доступ",
            ],
        ),
        (
            "Реестр решает",
            "neutral",
            [
                "неизвестное имя → отказ, вызов записан",
                "аргументы проверяются схемой",
                "повтор идентичного вызова → отказ",
                "таймаут и ретрай — свойство рантайма",
            ],
        ),
        (
            "Человек санкционирует",
            "attention",
            [
                "write-инструменты не видны планировщику",
                "граф встаёт на паузу, состояние сохраняется",
                "решение не несёт собственного действия",
                "право выдаётся на один шаг",
            ],
        ),
    ]

    for index, (title, tone, points) in enumerate(lanes):
        x = 32 + index * 313
        c.add(box(p, x, 84, 292, 44, title, tone=tone))
        cursor_y = 155
        for text in points:
            wrapped = wrap(text, 246, 12.5)
            c.add(f'<circle cx="{x + 18}" cy="{cursor_y - 4}" r="3" fill="{TONES[tone](p)[1]}"/>')
            for line_index, part in enumerate(wrapped):
                c.add(caption(p, x + 32, cursor_y + line_index * 16, part, muted=False))
            cursor_y += len(wrapped) * 16 + 14

    c.add(arrow(p, [(324, 106), (345, 106)], tone="accent"))
    c.add(arrow(p, [(637, 106), (658, 106)], tone="accent"))

    c.add(box(p, 32, 350, 936, 72, "", "", tone="danger", radius=10))
    c.add(
        caption(
            p,
            52,
            379,
            "agent_unapproved_writes_total обязан стоять на нуле.",
            muted=False,
            size=13,
            mono=True,
        )
    )
    c.add(
        caption(
            p,
            52,
            404,
            "Он выводится из записанных вызовов, а не из флага, и поднимает тревогу "
            "сразу, как только сдвинется.",
            size=12.5,
        )
    )
    c.add(
        caption(
            p,
            32,
            458,
            "Бюджеты: 12 вызовов · 30 шагов графа · 4 итерации цикла · 15 с на инструмент · "
            "2 идентичных вызова.",
            size=12.5,
        )
    )
    c.add(
        caption(
            p,
            32,
            482,
            "Никакого shell, исполнения произвольного кода и инструментов вне реестра.",
            size=12.5,
        )
    )
    return c


# ── Диаграмма 4: хранение данных ─────────────────────────────────────────────


def data_schema(p: Palette) -> Canvas:
    c = Canvas(1000, 520)

    c.add(heading(p, 32, 34, "Хранение данных"))
    c.add(
        caption(
            p,
            32,
            54,
            "Один файл SQLite, две независимые группы таблиц с разными владельцами схемы.",
        )
    )

    c.add(region(p, 32, 84, 936, 332, "ai_operations_agent.db · один файл (WAL)"))

    # Цвет — функция, а не украшение: тот же язык, что и в остальных схемах
    # (accent — состояние агента, success — проверенные данные, attention — человек,
    # neutral — идентичность и журнал, ghost — служебное).
    # Схема приложения: то, что отвечает на вопрос аудита «что агент сделал и на основании чего».
    c.add(region(p, 52, 120, 588, 276, "схема приложения — владелец: Alembic"))
    app_tables = (
        ("users", "neutral"),
        ("app_settings", "neutral"),
        ("alembic_version", "ghost"),
        ("agent_runs", "accent"),
        ("tool_calls", "success"),
        ("incident_analyses", "success"),
        ("approvals", "attention"),
        ("audit_events", "neutral"),
    )
    for index, (name, tone) in enumerate(app_tables):
        column, row = index % 3, index // 3
        c.add(box(p, 68 + column * 188, 164 + row * 56, 172, 44, name, tone=tone, mono=True))

    # Чекпоинтер: таблицы создаёт сам LangGraph, а не миграции.
    c.add(region(p, 664, 120, 288, 276, "чекпоинтер · LangGraph"))
    c.add(
        box(
            p,
            680,
            164,
            256,
            56,
            "checkpoints",
            "thread_id · checkpoint_id\nparent — цепочка версий",
            tone="accent",
            mono=True,
        )
    )
    c.add(box(p, 680, 236, 256, 56, "writes", "task_id · channel\nvalue", tone="accent", mono=True))
    c.add(caption(p, 680, 326, "BLOB = сериализованный AgentState", size=SMALL_SIZE))
    c.add(caption(p, 680, 346, "allowlist типов · app/agent/serde.py", size=SMALL_SIZE))

    c.add(
        caption(
            p,
            52,
            408,
            "API-движок (SQLAlchemy) и чекпоинтер открывают этот файл под WAL — это "
            "осознанный выбор.",
        )
    )

    c.add(
        caption(
            p,
            32,
            456,
            "Группы нельзя бэкапить по отдельности: backup и restore (manage.sh) снимают файл "
            "целиком и покрывают обе.",
        )
    )
    c.add(
        caption(
            p,
            32,
            482,
            "В серверном профиле те же две роли играют PostgreSQL: STORAGE_BACKEND=postgres и "
            "CHECKPOINTER=postgres.",
        )
    )
    return c


DIAGRAMS = {
    "architecture": (
        architecture,
        "Архитектура AI Operations Agent",
        "Агент живёт за FastAPI-сервисом и дотягивается до четырёх внешних систем "
        "через MCP-серверы; состояние в SQLite или PostgreSQL, телеметрия в Prometheus.",
    ),
    "workflow": (
        workflow,
        "Граф расследования",
        "Воркфлоу на LangGraph: детерминированный сбор и корреляция, затем ограниченный "
        "цикл выбора инструментов, затем подтверждение человеком перед любой записью.",
    ),
    "guardrails": (
        guardrails,
        "Чего агент не может",
        "Три слоя контроля: модель предлагает, реестр проверяет и отказывает, человек "
        "санкционирует каждую запись.",
    ),
    "schema": (
        data_schema,
        "Схема данных AI Operations Agent",
        "Один файл SQLite содержит две группы таблиц: схему приложения под Alembic и "
        "таблицы чекпоинтера LangGraph; бэкап охватывает обе.",
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("docs/assets"),
        help="Directory to write the SVG files into.",
    )
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    for name, (build, title, description) in DIAGRAMS.items():
        for palette in (LIGHT, DARK):
            svg = build(palette).render(palette, title=title, description=description)
            path = args.out / f"{name}-{palette.name}.svg"
            path.write_text(svg, encoding="utf-8")
            print(f"wrote {path}")


if __name__ == "__main__":
    main()
