"""The bundled browser interface and its stable integration points."""


async def test_root_serves_the_local_application_shell(http_client):
    response = await http_client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "AI Operations Agent" in response.text
    assert 'id="investigation-form"' in response.text
    assert 'id="approval"' in response.text
    assert 'id="history-list"' in response.text
    assert 'id="source-grid"' in response.text
    assert 'id="token"' not in response.text


async def test_static_assets_are_bundled_and_not_protected_by_api_auth(http_client):
    stylesheet = await http_client.get("/static/styles.css")
    script = await http_client.get("/static/app.js")

    assert stylesheet.status_code == 200
    assert "--accent" in stylesheet.text
    assert script.status_code == 200
    assert 'api("/runs"' in script.text
    assert "/approval" in script.text


def test_script_uses_text_escaping_for_remote_content():
    from app.web.routes import STATIC_DIR

    script = (STATIC_DIR / "app.js").read_text()
    assert "escapeHtml(item.summary)" in script
    assert "escapeHtml(run.task)" in script
    assert "escapeHtml(source.error" in script
