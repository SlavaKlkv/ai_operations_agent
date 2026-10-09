"""Встроенный браузерный интерфейс и его стабильные точки интеграции."""


async def test_root_serves_the_local_application_shell(http_client):
    response = await http_client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "AI Operations Agent" in response.text
    assert 'id="investigation-form"' in response.text
    assert 'id="approval"' in response.text
    assert 'id="history-list"' in response.text
    assert 'id="source-grid"' in response.text
    assert 'id="setup-overlay"' in response.text
    assert 'id="profile-grid"' in response.text
    assert 'id="token"' not in response.text


async def test_static_assets_are_bundled_and_not_protected_by_api_auth(http_client):
    stylesheet = await http_client.get("/static/styles.css")
    script = await http_client.get("/static/app.js")
    favicon = await http_client.get("/static/favicon.svg")

    assert favicon.status_code == 200
    assert stylesheet.status_code == 200
    assert "--accent" in stylesheet.text
    assert script.status_code == 200
    assert 'api("/runs"' in script.text
    assert "/approval" in script.text
    assert 'api("/setup")' in script.text
    assert 'api("/setup/model"' in script.text
    assert "data-download=" in script.text


def test_script_uses_text_escaping_for_remote_content():
    from app.web.routes import STATIC_DIR

    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "escapeHtml(item.summary)" in script
    assert "escapeHtml(run.task)" in script
    assert "escapeHtml(source.error" in script


def test_interface_exposes_run_detail_and_diagnostics():
    """Веха 2: у запуска есть ход, а у приложения — локальная диагностика."""
    from app.web.routes import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert 'data-view="diagnostics"' in html
    assert 'id="trace-list"' in html
    assert 'id="diagnostics-grid"' in html
    assert "loadDiagnostics" in script
    assert "/trace" in script


def test_sidebar_uses_an_internal_settings_arrow():
    from app.web.routes import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert 'data-view="settings">Настройки <span aria-hidden="true">→</span>' in html
    assert "Настройки <span>↗</span>" not in html
