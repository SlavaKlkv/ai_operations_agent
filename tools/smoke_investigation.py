"""End-to-end smoke: полное расследование против уже поднятого образа.

Проверяет, что на реальном контейнере проходит весь контур: расследование
останавливается на подтверждении, после подтверждения завершается, а повторное
подтверждение отклоняется. Используется в CI после старта production-образа.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

TASK = (
    "После последнего релиза billing-service резко выросло количество 5xx. "
    "Разберись и подготовь issue."
)


def _request(base, method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        base + path, data=data, method=method, headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return error.code, None


def main(base):
    status, run = _request(base, "POST", "/runs", {"task": TASK})
    assert status == 201, f"POST /runs -> {status}"
    assert run and run["status"] == "awaiting_approval", f"статус {run}"
    assert run["pending_approval"]["tool"] == "create_issue", "ожидался create_issue"
    run_id = run["id"]

    status, decided = _request(
        base, "POST", f"/runs/{run_id}/approval", {"approved": True, "note": "smoke"}
    )
    assert status == 200, f"подтверждение -> {status}"
    assert decided and decided["status"] == "completed", f"после подтверждения {decided}"

    status, _ = _request(base, "POST", f"/runs/{run_id}/approval", {"approved": True})
    assert status == 409, f"повторное подтверждение -> {status}, ожидалось 409"

    print(f"smoke ok: {run_id} прошёл расследование, подтверждение и защиту от повтора")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"))
