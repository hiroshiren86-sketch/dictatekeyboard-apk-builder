#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_project.py — Puerto fiel de la CELDA 6 del cuaderno de Colab.
Aplica al código fuente clonado:
  1. fragment-bump  → androidx.fragment:* = 1.3.0 (misma regex que el cuaderno)
  2. sherpa-onnx    → crea assets/sherpa-onnx/ + jniLibs/ y ejecuta el
                      fetch script oficial si existe (mismos candidatos)
  3. user patches   → .patch/.diff del repo de automatización (patch -p1 --forward)

Uso:
  python patch_project.py --project-root ./project --module app \
      [--patches-dir ./patches]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

FRAGMENT_MIN_VERSION = "1.3.0"

SHERPA_CANDIDATES = [
    "tools/fetch-sherpa-onnx.sh",
    "fetch-sherpa-onnx.sh",
    "setup-sherpa-onnx.sh",
    "setup_sherpa_onnx.sh",
    "scripts/install-sherpa-onnx.sh",
    "scripts/sherpa-onnx/setup.sh",
    "app/setup-sherpa-onnx.sh",
    "tools/setup-sherpa-onnx.sh",
    "tools/sherpa-onnx/fetch.sh",
]


def run(cmd, cwd=None, timeout=600):
    return subprocess.run(cmd, shell=isinstance(cmd, str), cwd=cwd,
                          capture_output=True, text=True, timeout=timeout)


def backup_file(target: Path) -> Path | None:
    if not target.is_file():
        return None
    bkp = target.with_suffix(target.suffix + f".bak.{int(time.time() * 1000)}")
    import shutil
    shutil.copy2(target, bkp)
    return bkp


# ---------------------------------------------------------------- fragment --
def module_build_file(root: Path, module: str) -> Path | None:
    md = root / module
    for name in ("build.gradle.kts", "build.gradle"):
        p = md / name
        if p.is_file():
            return p
    return None


def bump_fragment_version(root: Path, module: str) -> dict:
    bf = module_build_file(root, module)
    if bf is None:
        return {"patch": "fragment-bump", "applied": False, "reason": "no build file"}
    text = bf.read_text(errors="ignore")
    pat = re.compile(
        r'(androidx\.fragment:fragment(?:-ktx)?)\s*:\s*(["\'])([0-9]+(?:\.[0-9]+)*(?:[-+][\w.\-]+)?)\2'
    )
    bumped = False
    changes = []

    def repl(m):
        nonlocal bumped
        old = m.group(3)
        if old == FRAGMENT_MIN_VERSION:
            return m.group(0)
        bumped = True
        changes.append(f"{m.group(1)}: {old} -> {FRAGMENT_MIN_VERSION}")
        return f"{m.group(1)}:{m.group(2)}{FRAGMENT_MIN_VERSION}{m.group(2)}"

    new_text = pat.sub(repl, text)
    if bumped:
        bkp = backup_file(bf)
        bf.write_text(new_text)
        return {"patch": "fragment-bump", "applied": True, "changes": changes,
                "backup": str(bkp) if bkp else None}
    if "androidx.fragment" not in text:
        m = re.search(r"dependencies\s*\{", text)
        if m:
            insert_at = m.end()
            new_line = f'\nimplementation("androidx.fragment:fragment-ktx:{FRAGMENT_MIN_VERSION}")\n'
            bkp = backup_file(bf)
            bf.write_text(text[:insert_at] + new_line + text[insert_at:])
            return {"patch": "fragment-bump", "applied": True,
                    "changes": [f"added androidx.fragment:fragment-ktx:{FRAGMENT_MIN_VERSION}"],
                    "backup": str(bkp) if bkp else None}
    return {"patch": "fragment-bump", "applied": False,
            "reason": "already at target version or not present"}


# ---------------------------------------------------------------- sherpa ----
def find_sherpa_script(root: Path) -> Path | None:
    for c in SHERPA_CANDIDATES:
        p = root / c
        if p.is_file():
            return p
    for p in root.rglob("*.sh"):
        try:
            t = p.read_text(errors="ignore").lower()
        except Exception:
            continue
        if "sherpa" in t and ("onnx" in t or "fetch" in t or "install" in t):
            return p
    return None


def apply_sherpa_onnx(root: Path, module: str) -> dict:
    md = root / module
    if not md.is_dir():
        return {"patch": "sherpa-onnx", "applied": False, "reason": "module dir missing"}
    assets_dir = md / "src" / "main" / "assets" / "sherpa-onnx"
    jnilibs_dir = md / "src" / "main" / "jniLibs"
    created = []
    for d in (assets_dir, jnilibs_dir):
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            created.append(str(d.relative_to(root)))

    script_run = None
    script = find_sherpa_script(root)
    if script is not None:
        try:
            script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IREAD | stat.S_IWRITE)
            env = os.environ.copy()
            env["SHERPA_ONNX_MODULE_DIR"] = str(md)
            env["SHERPA_ONNX_ASSETS_DIR"] = str(assets_dir)
            env["SHERPA_ONNX_JNILIBS_DIR"] = str(jnilibs_dir)
            print(f"[sherpa] ejecutando {script.relative_to(root)} "
                  f"(descarga .so/.jar de GitHub; puede tardar varios minutos)…", flush=True)
            res = run(["bash", str(script)], cwd=root, timeout=1800)
            script_run = {"script": str(script.relative_to(root)), "rc": res.returncode,
                          "stderr_tail": res.stderr[-600:]}
            print(f"[sherpa] script {script.name} rc={res.returncode}", flush=True)
            if res.returncode != 0:
                print(f"[sherpa] STDERR: {res.stderr[-400:]}", flush=True)
        except Exception as exc:
            script_run = {"script": str(script), "error": str(exc)}
            print(f"[sherpa] ERROR: {exc}", flush=True)
    else:
        print("[sherpa] no se encontró script oficial de fetch; se continúa", flush=True)

    so_files = sorted(str(p.relative_to(md)) for p in jnilibs_dir.rglob("*.so")) \
        if jnilibs_dir.is_dir() else []
    asset_files = sorted(p.name for p in assets_dir.glob("*")) if assets_dir.is_dir() else []
    return {"patch": "sherpa-onnx", "applied": True, "created_dirs": created,
            "script_run": script_run, "so_files": so_files, "asset_files": asset_files}


# ------------------------------------------------------------ user patches --
def apply_user_patches(root: Path, patches_dir: Path | None) -> list:
    if patches_dir is None or not patches_dir.is_dir():
        return []
    log = []
    for p in sorted(patches_dir.iterdir()):
        if p.suffix not in (".patch", ".diff"):
            continue
        res = run(f'patch -p1 --forward --no-backup-if-mismatch < "{p}"', cwd=root)
        ok = res.returncode == 0
        already = ("already applied" in res.stderr.lower()) or ("Reversed" in res.stderr)
        log.append({"patch": p.name, "applied": ok,
                    "skipped": "already applied" if already and not ok else None,
                    "stderr": res.stderr[:300] if not ok and not already else None})
        print(f"[patch] {p.name}: {'applied' if ok else ('skipped (already applied)' if already else 'FAILED')}",
              flush=True)
    return log


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-root", required=True)
    ap.add_argument("--module", default="app")
    ap.add_argument("--patches-dir", default=None)
    args = ap.parse_args()

    root = Path(args.project_root).resolve()
    patches_dir = Path(args.patches_dir).resolve() if args.patches_dir else None

    log = {"applied": [], "details": {}}

    print("[patch] bumping androidx.fragment…", flush=True)
    frag = bump_fragment_version(root, args.module)
    log["details"]["fragment_bump"] = frag
    if frag.get("applied"):
        log["applied"].append("fragment-bump")
        print(f"[patch] fragment: {frag.get('changes', frag.get('reason'))}", flush=True)
    else:
        print(f"[patch] fragment: {frag.get('reason')}", flush=True)

    print("[patch] applying Sherpa-ONNX integration…", flush=True)
    sherpa = apply_sherpa_onnx(root, args.module)
    log["details"]["sherpa_onnx"] = sherpa
    log["applied"].append("sherpa-onnx")
    print(f"[sherpa] so_files={len(sherpa['so_files'])}", flush=True)

    user_log = apply_user_patches(root, patches_dir)
    log["details"]["user_patches"] = user_log
    for u in user_log:
        if u.get("applied"):
            log["applied"].append(u["patch"])

    print(json.dumps({"ok": True, "applied": log["applied"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
