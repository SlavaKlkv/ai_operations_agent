"""Записывает демо-ролик интерфейса AI Operations Agent в ``docs/assets``.

Запись ведётся браузером по настоящим событиям мыши и клавиатуры: скрипт
ничего не монтирует поверх готового видео и не подставляет результаты — в кадр
попадает то, что интерфейс действительно показал в ответ на действие. Описание
сценария, требований к кадру и порядка обновления ролика — в
``docs/demo-recording.md``.

Курсора операционной системы в кадре нет (пишется страница, а не экран),
поэтому указатель рисуется слоем внутри страницы. Слой не имитирует движение:
он повторяет координаты настоящего события ``mousemove`` — того же, что
вызывает наведение и клик. Формы указателя — покадровые вырезки с экрана
macOS; они лежат в ``docs/assets/cursor-shapes`` и в репозиторий не попадают
(исключены через ``.git/info/exclude``), как и в эталонном проекте.

Ролик начинается со знакомства с темами: тёмная тема держится секунду, затем
настоящим нажатием на переключатель включается светлая ещё на секунду, затем —
системная, после чего играется основной сценарий. Системная тема на стенде
разрешается в тёмную (браузер эмулирует тёмную настройку ОС), поэтому остальной
сценарий идёт в привычном тёмном виде. Все режимы живут в одном файле
``docs/assets/demo.mp4``.

Расследование против локальной Ollama идёт минуты, поэтому длительная
обработка не показывается целиком: в кадр попадают начало (панель «Выполняется»)
и результат, а сама пауза вырезается склейкой. Ничего не ускоряется и не
дорисовывается — между началом и результатом просто нет кадров.

Ролик укладывается в 40 секунд. Прокрутки — единственное движение, которым
можно растянуть ролик до цели, не замедляя паузы: если он длиннее, они ускоряются,
а если короче — замедляются (движение становится плавнее). Множитель выводится из
длительности отрезков и суммарного времени прокруток, поэтому результат
детерминирован. Ускорение и замедление делаются нарезкой по отметкам сценария, а
не перезаписью.

Перед первым запуском нужен браузер Playwright и ffmpeg::

    uv run playwright install chromium

Запись истории для экрана «История» (разные запросы, а не один и тот же) и
сама запись::

    uv run python -m tools.record_demo --seed
    uv run python -m tools.record_demo
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import math
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from playwright.sync_api import Locator, Page, ViewportSize, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
CURSOR_DIR = ROOT / "docs" / "assets" / "cursor-shapes"
ASSETS_DIR = ROOT / "docs" / "assets"

# Кадр 1280x800 без масштабирования: апскейла нет — всё, что видно в кадре,
# отрисовано в его собственном разрешении.
VIEWPORT: ViewportSize = {"width": 1280, "height": 800}
VIDEO_SIZE: ViewportSize = {"width": 1280, "height": 800}

# Запрос, который набирается в ролике: реальный инцидент демонстрационного мира.
REQUEST = "После релиза billing-service резко выросли 5xx. Разберись и подготовь issue."

# Прошлые расследования для экрана «История»: разные формулировки, а не один и
# тот же запрос, и разные исходы. Расследования запускаются по-настоящему —
# через тот же API, что и из интерфейса. Запрос про сервис без данных в источнике
# завершается разбором без вывода, что даёт естественную «Ошибка» в истории.
HISTORY_SEED: list[tuple[str, str | None]] = [
    (
        "Пользователи жалуются на ошибки оплаты с 14:30 UTC. Проверь billing-service.",
        "billing-service",
    ),
    ("После выката v1.8.4 в billing-service выросли 5xx и время ответа.", "billing-service"),
    ("billing-service: всплеск таймаутов и ошибок сразу после ночного деплоя.", "billing-service"),
    ("Разберись, почему search-service отдаёт 500 после вчерашнего релиза.", "search-service"),
]

# Прокрутка идёт мелкими равномерными шагами колеса: крупные шаги читаются как
# рывки, а обратная связь на каждом шаге сбивала бы темп. Скорость ~650 px/с —
# спокойная, без «прыжка» до низа.
SCROLL_STEP = 16
SCROLL_WAIT_MS = 24

# Пауза на каждом экране одинаковая: иначе один экран держится дольше другого.
SCREEN_HOLD = 2.0
# Пауза между секциями внутри одного экрана.
SECTION_PAUSE = 0.8

# Вступление ролика: сколько держится каждая тема до начала сценария. После
# светлой включается системная, и основной сценарий идёт уже в ней.
INTRO_DARK_HOLD = 1.0
INTRO_LIGHT_HOLD = 1.0

# Нижний предел множителя прокруток: если ролик короче цели, прокрутки
# замедляются не сильнее чем вдвое — иначе движение станет вялым.
SLOWEST_SCROLLS = 0.5

# Верхняя граница длительности ролика: если он длиннее, прокрутки ускоряются.
TARGET_DURATION = 40.0

# Размер указателя задаётся его долей от интерфейса, а не «натуральной»
# величиной вырезки. Указатель должен быть виден и примерно втрое ниже кнопки,
# поэтому вырезка (36x56 при двойном разрешении) даёт стрелку около 11x17 CSS px.
CURSOR_SCALE = 0.30

# Горячая точка каждой формы в долях от её размера: стрелка и рука указывают
# остриём, текстовая черта и ладонь — серединой.
CURSOR_SHAPES = {
    "default": ("default.png", 0.12, 0.08),
    "pointer": ("pointer.png", 0.35, 0.10),
    "text": ("text.png", 0.50, 0.50),
    "grabbing": ("grab.png", 0.50, 0.50),
}


@dataclass(frozen=True)
class Point:
    x: float
    y: float


def cursor_assets() -> dict[str, dict[str, object]]:
    assets: dict[str, dict[str, object]] = {}
    for name, (filename, hot_x, hot_y) in CURSOR_SHAPES.items():
        path = CURSOR_DIR / filename
        if not path.exists():
            raise SystemExit(
                f"Нет формы курсора {path}. Вырезки не хранятся в репозитории — "
                "их нужно положить в docs/assets/cursor-shapes перед записью."
            )
        data = base64.b64encode(path.read_bytes()).decode()
        assets[name] = {
            "src": f"data:image/png;base64,{data}",
            "hotX": hot_x,
            "hotY": hot_y,
        }
    return assets


CURSOR_LAYER_JS = """
(assets) => {
  const SCALE = __SCALE__;
  const layer = document.createElement('div');
  layer.id = '__demo_cursor';
  Object.assign(layer.style, {
    position: 'fixed',
    left: '0px',
    top: '0px',
    zIndex: '2147483647',
    pointerEvents: 'none',
    willChange: 'transform',
    transform: 'translate(-1000px, -1000px)',
  });

  const images = {};
  for (const [name, info] of Object.entries(assets)) {
    const img = new Image();
    img.src = info.src;
    Object.assign(img.style, { position: 'absolute', display: 'none' });
    img.onload = () => {
      // Вырезка снята в двойном разрешении: в CSS-пикселях она вдвое меньше.
      const w = img.naturalWidth * SCALE;
      const h = img.naturalHeight * SCALE;
      img.style.width = w + 'px';
      img.style.height = h + 'px';
      img.style.left = -(w * info.hotX) + 'px';
      img.style.top = -(h * info.hotY) + 'px';
    };
    images[name] = img;
    layer.append(img);
  }

  const show = (name) => {
    for (const [key, img] of Object.entries(images)) {
      img.style.display = key === name ? 'block' : 'none';
    }
  };
  show('default');

  // Слой живёт в documentElement с position: fixed, поэтому едет по настоящим
  // координатам mousemove и не уезжает при прокрутке. Init-скрипт выполняется
  // до разбора документа, поэтому слой цепляется при первой возможности.
  const mount = () => {
    const root = document.documentElement;
    if (root && !layer.isConnected) root.append(layer);
  };
  mount();
  document.addEventListener('DOMContentLoaded', mount);

  window.__demoCursor = {
    shape: 'default',
    move(x, y) {
      mount();
      layer.style.transform = `translate(${x}px, ${y}px)`;
      // Форма берётся из настоящего вычисленного стиля элемента под курсором —
      // той же, что показала бы система.
      const el = document.elementFromPoint(x, y);
      let shape = 'default';
      if (el) {
        const css = getComputedStyle(el).cursor;
        if (css === 'pointer') shape = 'pointer';
        else if (css === 'text') shape = 'text';
        else if (css === 'grab' || css === 'grabbing') shape = 'grabbing';
      }
      this.shape = shape;
      show(shape);
    },
  };

  document.addEventListener(
    'mousemove',
    (event) => window.__demoCursor.move(event.clientX, event.clientY),
    true,
  );
}
""".replace("__SCALE__", str(CURSOR_SCALE))


class Recorder:
    """Ведёт курсор, клавиатуру и прокрутку так, как это делает человек."""

    def __init__(self, page: Page, t0: float) -> None:
        self.page = page
        self.t0 = t0
        self.pos = Point(VIEWPORT["width"] * 0.5, VIEWPORT["height"] * 0.5)
        # Интервалы движения колеса (в секундах от t0): по ним прокрутки
        # ускоряются отдельно от пауз, если ролик не укладывается в лимит.
        self.scrolls: list[tuple[float, float]] = []

    # --- Курсор ---

    def move_to(self, x: float, y: float, duration: float = 0.26) -> None:
        """Подводит курсор с замедлением у цели.

        Равномерное движение сразу выдаёт автомат, поэтому скорость падает по
        мере приближения (ease-out), а шаг привязан к кадру записи.
        """
        start = self.pos
        steps = max(2, int(duration / 0.016))
        for i in range(1, steps + 1):
            t = i / steps
            eased = 1 - math.pow(1 - t, 3)
            self.page.mouse.move(start.x + (x - start.x) * eased, start.y + (y - start.y) * eased)
            self.page.wait_for_timeout(16)
        self.pos = Point(x, y)

    def pointer(self, x: float, y: float) -> None:
        """Ставит указатель коротким движением — без «телепорта» под элемент."""
        self.move_to(x, y, duration=0.18)

    def hover(self, selector: str, duration: float = 0.26, settle: float = 0.12) -> Locator:
        element = self.page.locator(selector).first
        element.wait_for(state="visible")
        box = element.bounding_box()
        if box is None:
            raise RuntimeError(f"Элемент {selector} не виден на странице")
        self.move_to(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, duration)
        self.pause(settle)
        return element

    def click(self, selector: str, duration: float = 0.26, settle: float = 0.12) -> None:
        self.hover(selector, duration, settle)
        self.page.mouse.down()
        self.page.wait_for_timeout(70)
        self.page.mouse.up()

    def type(self, selector: str, text: str, delay: float = 24) -> None:
        """Набирает текст посимвольно — мгновенная вставка в кадре видна."""
        self.click(selector, settle=0.18)
        self.page.wait_for_timeout(140)
        self.page.keyboard.type(text, delay=delay)

    # --- Прокрутка ---

    def _wheel_by(self, delta: float, step: int = SCROLL_STEP, wait: int = SCROLL_WAIT_MS) -> None:
        """Прокручивает окно на ``delta`` пикселей равномерными шагами колеса.

        Обратной связи на каждом шаге нет намеренно: запрос к странице между
        шагами делает паузы неровными, и прокрутка выглядит дёрганой. Дистанция
        считается один раз, поэтому у низа страницы колесо не крутится вхолостую
        — раньше именно это давало дрожь в конце.
        """
        begin = time.monotonic()
        direction = 1 if delta > 0 else -1
        remaining = abs(delta)
        while remaining > 0.5:
            amount = min(step, remaining)
            self.page.mouse.wheel(0, direction * amount)
            self.page.wait_for_timeout(wait)
            remaining -= amount
        self.scrolls.append((begin - self.t0, time.monotonic() - self.t0))

    def scroll_to(self, selector: str, align: float = 0.5, tolerance: float = 24) -> None:
        """Плавно подводит элемент к заданной доле высоты кадра.

        Дистанция считается и ограничивается максимумом прокрутки, а после
        каждого прохода позиция перечитывается. Повторное измерение нужно потому,
        что плавная прокрутка догоняет событие колеса не мгновенно: если мерить
        элемент, пока предыдущий проход ещё едет, дистанция выйдет заниженной.
        Когда упираемся в низ, смещение равно нулю, поэтому колесо больше не
        крутится — окно просто останавливается.
        """
        element = self.page.locator(selector).first
        element.wait_for(state="visible")
        self.page.wait_for_timeout(60)
        for _ in range(5):
            box = element.bounding_box()
            if box is None:
                return
            delta = box["y"] + box["height"] / 2 - VIEWPORT["height"] * align
            if abs(delta) <= tolerance:
                return
            info = self.page.evaluate(
                "() => ({y: window.scrollY,"
                " max: Math.max(0, document.documentElement.scrollHeight - window.innerHeight)})"
            )
            reach = min(max(info["y"] + delta, 0.0), info["max"]) - info["y"]
            if abs(reach) <= tolerance:
                return
            self._wheel_by(reach)
            self.page.wait_for_timeout(120)

    def scroll_top(self) -> None:
        """Возвращает окно к началу перед показом следующего экрана."""
        self.page.evaluate("() => window.scrollTo(0, 0)")

    def pause(self, seconds: float) -> None:
        self.page.wait_for_timeout(int(seconds * 1000))


def seed_history(url: str) -> None:
    """Запускает прошлые расследования через API, чтобы история была разной.

    Это те же настоящие расследования, что и из интерфейса: разные формулировки
    и разные исходы. Ничего в базу напрямую не пишется.
    """
    with httpx.Client(base_url=url, timeout=600.0) as client:
        for task, service in HISTORY_SEED:
            payload: dict[str, str] = {"task": task}
            if service:
                payload["target_service"] = service
            response = client.post("/runs", json=payload)
            response.raise_for_status()
            body = response.json()
            print(f"  [{body['status']:>18}] {task[:64]}")


def scenario(page: Page, url: str, t0: float) -> tuple[dict[str, float], list[tuple[float, float]]]:
    rec = Recorder(page, t0)
    started = time.monotonic()
    markers: dict[str, float] = {}

    def step(name: str) -> None:
        print(f"  {time.monotonic() - started:6.1f}s  {name}")

    # --- Начальное состояние: стартовый экран, выбранная тема, курсор слева от
    # формы и до первого действия неподвижен.
    step("goto")
    page.goto(f"{url}/", wait_until="load")
    page.wait_for_selector("#health-pill.ok", timeout=30_000)
    form = page.locator(".investigation-form").bounding_box()
    field = page.locator("#task").bounding_box()
    assert form is not None and field is not None
    rec.pos = Point(form["x"] - 34, field["y"] + field["height"] * 0.5)
    page.mouse.move(rec.pos.x, rec.pos.y)

    # Вступление: знакомство с темами до начала сценария — тёмная тема,
    # переключение на светлую настоящим нажатием и переход на системную. Всё до
    # отметки ``start`` (загрузка страницы) в ролик не попадает.
    step("intro dark")
    rec.pause(0.4)
    markers["start"] = time.monotonic() - t0
    rec.pause(INTRO_DARK_HOLD)

    step("intro light")
    rec.click('.theme-option[data-theme-mode="light"]', settle=0.2)
    rec.pause(INTRO_LIGHT_HOLD)

    step("intro system")
    rec.click('.theme-option[data-theme-mode="system"]', settle=0.2)
    rec.pause(0.4)

    # 1. Запрос набирается посимвольно, а не появляется целиком.
    step("type request")
    rec.type("#task", REQUEST)
    rec.pause(0.3)

    # 2. Отправка. Показываем начало обработки, а не всю паузу целиком.
    step("submit")
    rec.click("#investigation-form button.primary", settle=0.15)
    page.wait_for_selector("#run-panel:not(.hidden)", timeout=10_000)
    rec.scroll_to("#run-panel", align=0.45)
    rec.pause(1.3)
    markers["cut"] = time.monotonic() - t0

    # Длинное ожидание в кадр не попадает: между началом и результатом ролик
    # склеивается (см. convert()).
    step("wait")
    page.wait_for_selector("#result:not(.hidden)", state="visible", timeout=300_000)
    rec.scroll_to("#result-summary", align=0.28)
    rec.pause(SECTION_PAUSE)
    markers["result"] = time.monotonic() - t0

    # 3. Итог сверху вниз: симптомы, действия, доказательства, ход расследования.
    step("result")
    rec.scroll_to("#evidence-list", align=0.32)
    rec.pause(SECTION_PAUSE)
    rec.scroll_to("#approval", align=0.5)
    rec.pause(SECTION_PAUSE)
    rec.click("#approval summary", settle=0.25)
    rec.pause(SECTION_PAUSE)
    rec.scroll_to("#approve", align=0.78)
    rec.pause(SECTION_PAUSE)
    rec.click("#approve", settle=0.25)
    page.wait_for_selector("#approval.hidden", state="attached", timeout=60_000)
    rec.pause(SCREEN_HOLD)

    # 4. Экраны сверху вниз, как они идут в меню.
    step("history")
    rec.click(".nav-item[data-view='history']", settle=0.25)
    rec.scroll_top()
    page.wait_for_selector("#history-list .history-card", timeout=20_000)
    rec.scroll_to("#history-list .history-card", align=0.3)
    rec.pause(SCREEN_HOLD)

    step("diagnostics")
    rec.click(".nav-item[data-view='diagnostics']", settle=0.25)
    rec.scroll_top()
    page.wait_for_selector("#diagnostics-grid .source-card", timeout=20_000)
    rec.scroll_to("#diagnostics-grid", align=0.32)
    rec.pause(SECTION_PAUSE)
    rec.scroll_to(".diagnostics-report", align=0.8)
    rec.pause(SCREEN_HOLD)

    # 5. Настройки: секции 01–04 сверху вниз, до кнопки внизу.
    step("settings")
    rec.click(".nav-item[data-view='settings']", settle=0.25)
    rec.scroll_top()
    page.wait_for_selector("#profile-grid .profile-card", timeout=20_000)
    page.wait_for_selector("#settings-sources .source-card", timeout=20_000)
    page.wait_for_selector("#settings-storage .source-card", timeout=20_000)
    rec.hover("#model-setup .profile-card", settle=0.4)
    rec.scroll_to(".github-setup", align=0.22)
    rec.pause(SECTION_PAUSE)
    rec.scroll_to("#settings-sources", align=0.28)
    rec.pause(SECTION_PAUSE)
    rec.scroll_to("#settings-storage", align=0.36)
    rec.pause(SECTION_PAUSE)
    rec.scroll_to(".setup-actions", align=0.85)
    rec.pause(SCREEN_HOLD)

    # 6. Возврат на главный экран по логотипу; запись завершается здесь.
    step("home")
    rec.click(".brand", settle=0.3)
    page.wait_for_selector("#view-investigate.active", timeout=20_000)
    page.wait_for_selector("#health-pill.ok", timeout=20_000)
    # После перезагрузки слой курсора ещё не получал mousemove — показываем его.
    rec.pointer(VIEWPORT["width"] * 0.55, VIEWPORT["height"] * 0.5)
    rec.pause(1.0)
    markers["end"] = time.monotonic() - t0

    return markers, rec.scrolls


def kept_intervals(markers: dict[str, float]) -> list[tuple[float, float]]:
    """Отрезки записи, попадающие в ролик: до отправки и после результата.

    Между отправкой и результатом расследование идёт минуты — кадры этой паузы
    не показываются, поэтому между отрезками склейка, а не ускорение.
    """
    return [(markers["start"], markers["cut"]), (markers["result"], markers["end"])]


def _scroll_spans(
    markers: dict[str, float], scrolls: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Прокрутки, оставшиеся в ролике, с обрезкой по его отрезкам."""
    spans: list[tuple[float, float]] = []
    for begin, end in scrolls:
        for a, b in kept_intervals(markers):
            left, right = max(begin, a), min(end, b)
            if right > left:
                spans.append((left, right))
    return sorted(spans)


def speed_factor(markers: dict[str, float], scrolls: list[tuple[float, float]]) -> float:
    """Во сколько раз ускорить прокрутки, чтобы уложиться в ``TARGET_DURATION``.

    Длительность без множителя равна сумме отрезков ролика, а прокрутки дают
    вклад ``scroll_total / speed``. Множитель выводится из равенства итога цели:
    ``speed = scroll_total / (scroll_total - (base - target))``. Знак разницы задаёт
    направление: ролик длиннее цели — ``speed > 1`` (прокрутки ускоряются), короче
    — ``speed < 1`` (замедляются и идут плавнее). Замедление ограничено снизу
    ``SLOWEST_SCROLLS``. Если прокруток нет или ускорением разницу не закрыть,
    множителя нет.
    """
    base = sum(b - a for a, b in kept_intervals(markers))
    total = sum(b - a for a, b in _scroll_spans(markers, scrolls))
    shortfall = base - TARGET_DURATION
    if shortfall > 0:
        if total <= shortfall:
            return 1.0
        return total / (total - shortfall)
    if shortfall < 0 and total > 0:
        return max(total / (total - shortfall), SLOWEST_SCROLLS)
    return 1.0


def _segments(
    markers: dict[str, float], scrolls: list[tuple[float, float]]
) -> list[tuple[float, float, bool]]:
    """Разбивает ролик на отрезки: паузы (в темпе) и прокрутки (ускоряются)."""
    spans = _scroll_spans(markers, scrolls)
    segments: list[tuple[float, float, bool]] = []
    for a, b in kept_intervals(markers):
        cursor = a
        for begin, end in spans:
            begin, end = max(begin, a), min(end, b)
            if end <= begin:
                continue
            if begin > cursor:
                segments.append((cursor, begin, False))
            segments.append((begin, end, True))
            cursor = end
        if cursor < b:
            segments.append((cursor, b, False))
    return segments


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(out.stdout.strip())


def convert(
    webm: Path,
    output: Path,
    markers: dict[str, float],
    scrolls: list[tuple[float, float]],
) -> None:
    """Перекодирует запись в H.264, вырезая паузу ожидания расследования.

    Первые кадры (загрузка страницы) отбрасываются: ролик начинается на готовом
    экране вместе с курсором. Между началом обработки (``cut``) и результатом
    (``result``) кадров нет — это склейка, а не ускорение, поэтому ничего не
    дёргается. Прокрутки при необходимости ускоряются одним множителем, а паузы
    и остальное движение остаются в настоящем темпе.
    """
    speed = speed_factor(markers, scrolls)
    segments = _segments(markers, scrolls)
    count = len(segments)
    parts = [f"[0:v]split={count}" + "".join(f"[c{i}]" for i in range(count))]
    labels = []
    for i, (begin, end, is_scroll) in enumerate(segments):
        pts = f"(PTS-STARTPTS)/{speed:.4f}" if is_scroll and speed != 1 else "PTS-STARTPTS"
        parts.append(f"[c{i}]trim=start={begin:.3f}:end={end:.3f},setpts={pts},fps=30[o{i}]")
        labels.append(f"[o{i}]")
    parts.append("".join(labels) + f"concat=n={count}:v=1:a=0,format=yuv420p[out]")
    filter_complex = ";".join(parts)

    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(webm),
            "-filter_complex",
            filter_complex,
            "-map",
            "[out]",
            "-an",
            "-c:v",
            "libx264",
            "-crf",
            "23",
            "-preset",
            "slow",
            "-movflags",
            "+faststart",
            str(output),
        ],
        check=True,
    )
    print(f"  длительность {probe_duration(output):.1f}s, прокрутки x{speed:.2f}")


def record(url: str, output: Path, headed: bool, keep_webm: bool) -> None:
    assets = cursor_assets()
    videos = output.parent / "_recording" / "video"
    shutil.rmtree(videos.parent, ignore_errors=True)
    videos.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=not headed,
            args=["--hide-scrollbars"],
        )
        context = browser.new_context(
            viewport=VIEWPORT,
            record_video_dir=str(videos),
            record_video_size=VIDEO_SIZE,
            locale="ru-RU",
            # Тема живёт в localStorage и применяется до первой отрисовки, поэтому
            # кадр не мигает белым. Ролик начинается тёмной темой, дальше сценарий
            # сам проходит светлую и системную. Системная разрешается в тёмную:
            # браузер эмулирует тёмную настройку ОС, поэтому переход на неё
            # заметен после светлой, а основной сценарий идёт в привычном виде.
            color_scheme="dark",
            storage_state={
                "cookies": [],
                "origins": [
                    {
                        "origin": url,
                        "localStorage": [{"name": "aoa-theme", "value": "dark"}],
                    }
                ],
            },
        )
        context.add_init_script(f"({CURSOR_LAYER_JS})({json.dumps(assets)})")

        page = context.new_page()
        t0 = time.monotonic()
        result: tuple[dict[str, float], list[tuple[float, float]]] | None = None
        try:
            result = scenario(page, url, t0)
        finally:
            video = page.video
            context.close()
            browser.close()
            if video is not None and result is not None:
                markers, scrolls = result
                source = Path(video.path())
                convert(source, output, markers, scrolls)
                if not keep_webm:
                    shutil.rmtree(videos, ignore_errors=True)
                    # Оставляем после записи только готовый mp4.
                    with contextlib.suppress(OSError):
                        videos.parent.rmdir()
                print(f"Готово: {output}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000", help="адрес стенда")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--headed", action="store_true", help="запускать браузер с окном")
    parser.add_argument("--keep-webm", action="store_true")
    parser.add_argument(
        "--seed",
        action="store_true",
        help="наполнить историю разными расследованиями и не записывать ролик",
    )
    args = parser.parse_args()

    if args.seed:
        seed_history(args.url)
        return 0

    if shutil.which("ffmpeg") is None:
        raise SystemExit("Нужен ffmpeg: запись пишется в webm и перекодируется в mp4")

    output = args.output or ASSETS_DIR / "demo.mp4"
    record(args.url, output, args.headed, args.keep_webm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
