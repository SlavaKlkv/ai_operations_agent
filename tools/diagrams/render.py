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
) -> str:
    fill, stroke, text = TONES[tone](p)
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    lines = [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"{dash}/>'
    ]
    cx = x + w / 2
    if subtitle:
        wrapped = wrap(subtitle, w - 20, 11.5)
        # Заголовок и подзаголовок центрируются единым блоком, чтобы подпись
        # из трёх строк оставалась внутри блока и не выходила за нижнюю границу.
        top = y + h / 2 - (len(wrapped) * 14) / 2
        lines.append(
            f'<text x="{cx}" y="{top}" text-anchor="middle" font-size="14" '
            f'font-weight="600" fill="{text}"'
            + (f' font-family="{MONO}"' if mono else "")
            + f">{escape(title)}</text>"
        )
        for index, part in enumerate(wrapped):
            lines.append(
                f'<text x="{cx}" y="{top + 17 + index * 14}" text-anchor="middle" '
                f'font-size="11.5" fill="{p.muted}">{escape(part)}</text>'
            )
    else:
        lines.append(
            f'<text x="{cx}" y="{y + h / 2 + 5}" text-anchor="middle" font-size="14" '
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
        f'<text x="{x + 14}" y="{y + 19}" font-size="11" font-weight="600" '
        f'letter-spacing="0.6" fill="{p.muted}">{escape(label.upper())}</text>'
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
            f'font-size="11" fill="{p.muted}">{escape(label)}</text>'
        )
    return markup


def caption(
    p: Palette,
    x: float,
    y: float,
    text: str,
    *,
    anchor: str = "start",
    size: float = 11.5,
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
        f'<text x="{x}" y="{y}" font-size="12" font-weight="700" letter-spacing="0.7" '
        f'fill="{p.muted}">{escape(text.upper())}</text>'
    )


# ── Диаграмма 1: архитектура ────────────────────────────────────────────────


def architecture(p: Palette) -> Canvas:
    c = Canvas(1000, 620)

    c.add(heading(p, 32, 34, "AI Operations Agent — архитектура системы"))
    c.add(
        caption(
            p, 32, 54, "Всё, до чего агент дотягивается, и что стоит между ним и каждой системой."
        )
    )

    c.add(box(p, 32, 78, 190, 54, "Инженер", "задача на естественном языке", tone="accent"))
    c.add(arrow(p, [(127, 132), (127, 168)]))

    # Слой API
    c.add(region(p, 32, 168, 390, 120, "FastAPI"))
    c.add(box(p, 48, 196, 172, 34, "POST /runs", mono=True, radius=6))
    c.add(box(p, 48, 238, 172, 34, "POST /approval", mono=True, radius=6, tone="attention"))
    c.add(box(p, 234, 196, 172, 34, "GET /runs/{id}/trace", mono=True, radius=6))
    c.add(box(p, 234, 238, 172, 34, "GET /metrics", mono=True, radius=6))

    # Ядро агента
    c.add(region(p, 32, 308, 390, 168, "воркфлоу LangGraph"))
    c.add(box(p, 48, 336, 172, 46, "Состояние", "типизированное, в чекпоинте", tone="neutral"))
    c.add(box(p, 234, 336, 172, 46, "Ограничения", "бюджеты · allowlist · чтение/запись"))
    c.add(
        box(
            p,
            48,
            396,
            172,
            60,
            "Планировщик",
            "выбирает следующий инструмент\nиз разрешённых",
            tone="accent",
        )
    )
    c.add(
        box(
            p,
            234,
            396,
            172,
            60,
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
            502,
            390,
            56,
            "LLM  ·  LangChain",
            "необязателен: без него агент идёт детерминированным путём",
            tone="ghost",
            dashed=True,
        )
    )
    c.add(arrow(p, [(227, 476), (227, 502)], dashed=True))

    c.add(arrow(p, [(227, 288), (227, 308)], tone="accent"))

    # Граница MCP
    c.add(arrow(p, [(422, 392), (486, 392)], "MCP", tone="accent"))
    c.add(region(p, 486, 168, 482, 308, "слой интеграции MCP"))
    c.add(
        box(
            p,
            504,
            196,
            446,
            60,
            "Пул MCP-клиентов",
            "находит инструменты · чтение/запись по аннотациям · деградирует по серверам",
            tone="accent",
        )
    )

    servers = [
        ("monitoring", "метрики · алерты\nагрегированные ошибки", "success"),
        ("code", "деплои\nкоммиты · PR", "success"),
        ("incident", "задачи\nсоздание · комментарии", "danger"),
        ("knowledge", "рунбуки\nпоиск", "success"),
    ]
    for index, (name, detail, tone) in enumerate(servers):
        x = 504 + index * 113
        c.add(box(p, x, 268, 103, 84, name, detail, tone=tone, mono=True))
        c.add(arrow(p, [(x + 51, 256), (x + 51, 268)]))
        c.add(box(p, x, 372, 103, 44, "внешняя", "система", tone="ghost", dashed=True))
        c.add(arrow(p, [(x + 51, 352), (x + 51, 372)], dashed=True))

    c.add(
        caption(
            p,
            727,
            444,
            "каждый сервер — отдельный процесс, говорит по MCP через stdio",
            anchor="middle",
        )
    )

    # Хранилище и наблюдаемость
    c.add(region(p, 486, 496, 482, 96, "состояние и телеметрия"))
    c.add(box(p, 504, 524, 140, 50, "PostgreSQL", "запуски · подтверждения\nаудит · чекпоинты"))
    c.add(box(p, 656, 524, 140, 50, "Prometheus", "стоимость запуска\nбезопасность записи"))
    c.add(box(p, 808, 524, 142, 50, "Grafana", "дашборд\nи алерты"))
    c.add(arrow(p, [(422, 254), (486, 254)], "", tone="muted"))
    c.add(arrow(p, [(422, 520), (486, 540)]))

    return c


# ── Диаграмма 2: граф воркфлоу ──────────────────────────────────────────────


def workflow(p: Palette) -> Canvas:
    c = Canvas(1000, 700)

    c.add(heading(p, 32, 34, "Граф расследования"))
    c.add(
        caption(
            p,
            32,
            54,
            "Сначала детерминированная работа, затем ограниченный агентный цикл, затем человек.",
        )
    )

    w, h = 224, 50
    main = 300  # левая колонка: расследование
    right = 676  # правая колонка: вывод и действие
    mid_l, mid_r = main + w / 2, right + w / 2

    def node(x, y, title, subtitle="", tone="neutral"):
        c.add(box(p, x, y, w, h, title, subtitle, tone=tone, mono=True))

    # ── Левая колонка ───────────────────────────────────────────────────────
    c.add(box(p, mid_l - 38, 82, 76, 28, "START", tone="ghost", radius=14))
    c.add(arrow(p, [(mid_l, 110), (mid_l, 132)]))

    node(main, 132, "analyze_task", "сервис и временное окно")
    c.add(arrow(p, [(mid_l, 182), (mid_l, 206)]))

    node(main, 206, "collect_initial_context", "метрики · деплои · ошибки · алерты")
    c.add(arrow(p, [(main - 4, 231), (main - 76, 231)], tone="danger"))
    c.add(caption(p, main - 40, 222, "нет сигнала", anchor="middle", size=10.5))
    c.add(
        box(
            p,
            24,
            206,
            200,
            50,
            "insufficient_context",
            "останавливается и объясняет",
            tone="danger",
            mono=True,
        )
    )
    c.add(arrow(p, [(mid_l, 256), (mid_l, 280)]))

    node(main, 280, "correlate", "всплеск ↔ деплой ↔ коммит", tone="success")
    c.add(arrow(p, [(mid_l, 330), (mid_l, 362)]))

    # ── Цикл ────────────────────────────────────────────────────────────────
    c.add(region(p, main - 116, 352, w + 148, 264, "агентный цикл — ограниченный"))
    node(main, 382, "select_tool", "единственный реальный выбор модели", tone="accent")
    c.add(arrow(p, [(mid_l, 432), (mid_l, 458)]))
    node(main, 458, "execute_tool", "проверен · по таймауту · записан")
    c.add(arrow(p, [(mid_l, 508), (mid_l, 534)]))
    node(main, 534, "evaluate_observation", "это что-то изменило?")

    c.add(
        arrow(
            p,
            [(main, 559), (main - 92, 559), (main - 92, 407), (main, 407)],
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
            "выходы: ничего не запрошено · бюджет исчерпан · нет прогресса · 4 итерации",
            size=10.5,
        )
    )

    # ── Правая колонка, читается снизу вверх ────────────────────────────────
    c.add(arrow(p, [(main + w, 559), (right + 14, 559)], "достаточно", label_dy=-9))
    node(right, 534, "generate_analysis", "структурирован · заземлён", tone="success")
    c.add(arrow(p, [(mid_r, 534), (mid_r, 504)]))

    node(right, 454, "propose_action", "уверенность ≥ 0.6, иначе ничего")
    c.add(arrow(p, [(mid_r, 454), (mid_r, 424)], tone="danger"))
    c.add(caption(p, mid_r + 10, 443, "предложена запись", size=10.5))
    c.add(
        arrow(
            p,
            [(right, 479), (right - 40, 479), (right - 40, 190), (right + 4, 190)],
            "",
            tone="muted",
        )
    )
    c.add(caption(p, right - 50, 300, "писать нечего", anchor="end", size=10.5))

    node(right, 374, "request_approval", "пауза · состояние в чекпоинте", tone="attention")
    c.add(arrow(p, [(mid_r, 374), (mid_r, 344)], tone="danger"))
    c.add(caption(p, mid_r + 10, 363, "подтверждено", size=10.5))
    c.add(
        arrow(
            p,
            [(right + w, 399), (right + w + 40, 399), (right + w + 40, 190), (right + w - 4, 190)],
            "",
        )
    )
    c.add(caption(p, right + w + 34, 300, "отклонено", anchor="end", size=10.5))

    node(right, 294, "execute_action", "один инструмент · один шаг", tone="danger")
    c.add(arrow(p, [(mid_r, 294), (mid_r, 240)]))

    node(right, 190, "final_response", "что сделал и чего не сделал")
    c.add(arrow(p, [(mid_r, 190), (mid_r, 164)]))
    c.add(box(p, mid_r - 32, 136, 64, 28, "END", tone="ghost", radius=14))

    # ── Подробное описание барьера ─────────────────────────────────────────
    c.add(box(p, 32, 634, 936, 48, "", "", tone="attention", radius=10))
    c.add(
        caption(
            p,
            52,
            656,
            "Пауза долговечная: состояние лежит в чекпоинте, поэтому решение приходит "
            "отдельным HTTP-запросом от отдельного человека —",
            muted=False,
            size=12,
        )
    )
    c.add(
        caption(
            p,
            52,
            673,
            "а выполняется то действие, которое сохранил граф, а не то, что несёт "
            "подтверждающий запрос.",
            size=11.5,
        )
    )
    return c


# ── Диаграмма 3: что останавливает агента ───────────────────────────────────


def guardrails(p: Palette) -> Canvas:
    c = Canvas(1000, 430)

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
        c.add(box(p, x, 84, 292, 40, title, tone=tone))
        for line, text in enumerate(points):
            y = 148 + line * 30
            c.add(f'<circle cx="{x + 18}" cy="{y - 4}" r="3" fill="{TONES[tone](p)[1]}"/>')
            c.add(caption(p, x + 32, y, text, muted=False, size=12))

    c.add(arrow(p, [(324, 104), (345, 104)], tone="accent"))
    c.add(arrow(p, [(637, 104), (658, 104)], tone="accent"))

    c.add(box(p, 32, 296, 936, 56, "", "", tone="danger", radius=10))
    c.add(
        caption(
            p,
            52,
            320,
            "agent_unapproved_writes_total обязан стоять на нуле.",
            muted=False,
            size=12.5,
            mono=True,
        )
    )
    c.add(
        caption(
            p,
            52,
            339,
            "Он выводится из записанных вызовов, а не из флага, и поднимает тревогу "
            "сразу, как только сдвинется.",
            size=11.5,
        )
    )
    c.add(
        caption(
            p,
            32,
            384,
            "Бюджеты: 12 вызовов · 30 шагов графа · 4 итерации цикла · 15 с на инструмент · "
            "2 идентичных вызова.",
            size=12,
        )
    )
    c.add(
        caption(
            p,
            32,
            404,
            "Никакого shell, исполнения произвольного кода и инструментов вне реестра.",
            size=12,
        )
    )
    return c


DIAGRAMS = {
    "architecture": (
        architecture,
        "Архитектура AI Operations Agent",
        "Агент живёт за FastAPI-сервисом и дотягивается до четырёх внешних систем "
        "через MCP-серверы; состояние в PostgreSQL, телеметрия в Prometheus.",
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
