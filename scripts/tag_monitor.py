#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tag_monitor.py — Detección del "último tag" del repo DevEmperor/DictateKeyboard.

Réplica EXACTA de la lógica de la Celda 5 del cuaderno de Colab
(`_remote_latest_tag`): git ls-remote --tags + orden semver. Así el monitor
y el cuaderno SIEMPRE resuelven el mismo tag.

Subcomandos:
  check                → detecta el tag actual, lo compara con state/state.json
                         y decide si hay que compilar. Si decide compilar,
                         actualiza state.json (pendiente) y lo imprime.
  finish --status X    → marca el build terminado (success|failure) y
                         contadores de reintento.
  show                 → imprime el estado actual.

Uso en Actions (monitor):
  python scripts/tag_monitor.py check > decision.json
  (el YAML lee decision.json con jq)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- Configuración del proyecto vigilado ----------------------------------
REPO_URL = "https://github.com/DevEmperor/DictateKeyboard"
STATE_FILE = Path(
    os.environ.get("TAGMON_STATE_FILE",
                   str(Path(__file__).resolve().parent.parent / "state" / "state.json"))
)
DEFAULT_MAX_RETRIES = 2
# ---------------------------------------------------------------------------


def ver_key(name: str) -> tuple:
    """Orden semver idéntico al de la Celda 5 del cuaderno."""
    core = name.lstrip("vV").split("-")[0]
    return tuple(int(c) if c.isdigit() else 0 for c in core.split("."))


def remote_latest_tag() -> tuple[str, str]:
    """Devuelve (tag, sha_de_commit) del mayor tag semántico del remoto."""
    out = subprocess.run(
        ["git", "ls-remote", "--tags", REPO_URL],
        capture_output=True, text=True, timeout=180, check=True,
    )
    names: dict[str, str] = {}
    for line in out.stdout.splitlines():
        if "refs/tags/" not in line:
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, ref = parts
        name = ref.split("refs/tags/")[-1].strip()
        if name.endswith("^{}"):
            # tag anotado "peeled": apunta al commit real; gana sobre el objeto
            name = name[:-3]
        names[name] = sha
    if not names:
        return "", ""
    tag = max(names, key=ver_key)
    return tag, names[tag]


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "latest_tag": "",
        "latest_sha": "",
        "build_status": "idle",   # idle|pending|success|failure
        "build_tag": "",
        "retries": 0,
        "max_retries": DEFAULT_MAX_RETRIES,
        "history": [],
    }


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def push_history(state: dict, event: str, detail: dict) -> None:
    hist = state.setdefault("history", [])
    hist.append({"at": now_iso(), "event": event, **detail})
    del hist[:-25]  # mantener los últimos 25 eventos


def cmd_check() -> int:
    state = load_state()
    tag, sha = remote_latest_tag()
    state["last_check"] = now_iso()

    changed = (tag != state.get("latest_tag")) or (sha != state.get("latest_sha"))
    failed_retryable = (
        state.get("build_status") == "failure"
        and state.get("build_tag") == tag
        and int(state.get("retries", 0)) < int(state.get("max_retries", DEFAULT_MAX_RETRIES))
    )

    should_build = bool(changed or failed_retryable)
    reason = (
        f"tag_nuevo {state.get('latest_tag') or '(ninguno)'} -> {tag}"
        if changed
        else (f"reintento_{state.get('retries', 0)}" if failed_retryable else "sin_cambios")
    )

    if changed:
        state["latest_tag"] = tag
        state["latest_sha"] = sha
        state["build_status"] = "pending"
        state["build_tag"] = tag
        state["retries"] = 0
        state["queued_at"] = now_iso()
        push_history(state, "queued", {"tag": tag, "sha": sha[:12], "reason": reason})
    elif failed_retryable:
        push_history(state, "requeued", {"tag": tag, "reason": reason})

    save_state(state)

    decision = {
        "should_build": should_build,
        "reason": reason,
        "tag": tag,
        "sha": sha,
        "retries": state.get("retries", 0),
        "build_status": state.get("build_status"),
    }
    print(json.dumps(decision, indent=2, ensure_ascii=False))
    return 0


def cmd_finish(status: str) -> int:
    state = load_state()
    state["build_status"] = "success" if status == "success" else "failure"
    state["finished_at"] = now_iso()
    if status != "success":
        state["retries"] = int(state.get("retries", 0)) + 1
    else:
        state["retries"] = 0
    push_history(
        state,
        "build_finished",
        {"tag": state.get("build_tag", ""), "status": state["build_status"],
         "retries": state.get("retries", 0)},
    )
    save_state(state)
    print(json.dumps({"ok": True, "status": state["build_status"]}))
    return 0


def cmd_init() -> int:
    """Primer arranque: fija el tag actual SIN encolar build."""
    state = load_state()
    tag, sha = remote_latest_tag()
    state["latest_tag"], state["latest_sha"] = tag, sha
    state["build_status"] = "idle"
    push_history(state, "init", {"tag": tag, "sha": sha[:12]})
    save_state(state)
    print(json.dumps({"ok": True, "initialized_tag": tag}, indent=2))
    return 0


def cmd_latest() -> int:
    """Imprime 'TAG SHA' del mayor tag semver (para build.yml)."""
    tag, sha = remote_latest_tag()
    print(f"{tag} {sha}")
    return 0


def cmd_show() -> int:
    print(json.dumps(load_state(), indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Monitor del último tag semver")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("init")
    sub.add_parser("latest")
    fin = sub.add_parser("finish")
    fin.add_argument("--status", choices=["success", "failure"], required=True)
    sub.add_parser("show")
    args = ap.parse_args()

    if args.cmd == "check":
        return cmd_check()
    if args.cmd == "init":
        return cmd_init()
    if args.cmd == "latest":
        return cmd_latest()
    if args.cmd == "finish":
        return cmd_finish(args.status)
    return cmd_show()


if __name__ == "__main__":
    sys.exit(main())
