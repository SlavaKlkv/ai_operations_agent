"""Инварианты поставки для пользовательского Compose-комплекта и релизного конвейера.

Пользователь никогда не видит исходники на Python; он видит compose.yaml из
GitHub Release. Эти проверки держат эту поверхность честной: порт остаётся на
loopback, локальный профиль поставляется без PostgreSQL и Redis, а тег
выпущенного образа совпадает с версией проекта.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "compose.yaml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

# Файлы, которые релиз складывает в скачиваемый комплект пользователя.
BUNDLE_FILES = (
    "compose.yaml",
    "start.sh",
    "start.ps1",
    "manage.sh",
    "manage.ps1",
    "README.md",
    "LICENSE",
)


def _project_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match is not None, "pyproject.toml must declare a version"
    return match.group(1)


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def _workflow() -> dict:
    data = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML читает ключ `on` как boolean True по правилам YAML 1.1.
    data["triggers"] = data.get("on", data.get(True))
    return data


def test_web_port_is_bound_to_loopback_only():
    assert _compose()["services"]["app"]["ports"] == ["127.0.0.1:8000:8000"]


def test_single_service_with_a_persistent_volume():
    compose = _compose()
    assert list(compose["services"]) == ["app"]
    assert "app-data" in compose["volumes"]
    app_volumes = compose["services"]["app"]["volumes"]
    assert any(volume.startswith("app-data:/srv/app/data") for volume in app_volumes)


def test_local_profile_ships_without_postgres_or_redis():
    text = COMPOSE_FILE.read_text(encoding="utf-8").lower()
    assert "postgres" not in text
    assert "redis" not in text


def test_service_restarts_and_reports_health():
    app = _compose()["services"]["app"]
    assert app["restart"] == "unless-stopped"
    assert "healthcheck" in app


def test_default_image_matches_the_released_version():
    image = _compose()["services"]["app"]["image"]
    assert f"ghcr.io/slavaklkv/ai-operations-agent:{_project_version()}" in image


def test_release_bundle_files_exist_and_shell_scripts_are_executable():
    for name in BUNDLE_FILES:
        assert (REPO_ROOT / name).is_file(), f"missing bundle file: {name}"
    assert os.access(REPO_ROOT / "start.sh", os.X_OK)
    assert os.access(REPO_ROOT / "manage.sh", os.X_OK)


def test_release_workflow_publishes_a_signed_multi_arch_image():
    workflow = _workflow()
    assert "v*" in workflow["triggers"]["push"]["tags"]

    image_job = workflow["jobs"]["image"]
    build = next(
        step
        for step in image_job["steps"]
        if str(step.get("uses", "")).startswith("docker/build-push-action")
    )
    assert build["with"]["push"] is True
    assert build["with"]["platforms"] == "linux/amd64,linux/arm64"
    assert build["with"]["provenance"] is True
    assert build["with"]["sbom"] is True

    signer = next(
        step
        for step in image_job["steps"]
        if str(step.get("uses", "")).startswith("sigstore/cosign-installer")
    )
    assert signer  # подпись обязательна для релизного образа

    release_job = workflow["jobs"]["release"]
    assert release_job["needs"] == "image"


def test_release_bundle_step_ships_every_user_file():
    assemble = next(
        step
        for step in _workflow()["jobs"]["release"]["steps"]
        if step.get("name") == "Assemble the user-facing bundle"
    )
    for name in BUNDLE_FILES:
        assert name in assemble["run"], f"release bundle omits {name}"
    assert "SHA256SUMS.txt" in assemble["run"]
