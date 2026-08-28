#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_and_sign.py — Puerto fiel de las CELDAS 9 y 10 del cuaderno de Colab.

Celda 9: ./gradlew {module}:assembleRelease --no-daemon --stacktrace
         con reintento (4 intentos, backoff exponencial) y AUTO-FIX de lint
         ("Upgrade X version to at least Y.Z" → bump de la dependencia).
Celda 10: zipalign -p 4 + apksigner sign --ks … + apksigner verify.

Uso:
  python build_and_sign.py --project-root ./project --module app \
      --task assembleRelease --keystore ./keystore.jks \
      --ks-pass "$KEYSTORE_PASSWORD" --ks-alias "$KEYSTORE_ALIAS" \
      --out-dir ./artifacts --tag v6.1.0
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

MAX_RETRIES = 4
BACKOFF = 2

_LINT_HINTS = [
    ("fragment", ["androidx.fragment:fragment", "androidx.fragment:fragment-ktx",
                  "androidx.fragment"], "1.3.0"),
    ("activity", ["androidx.activity:activity", "androidx.activity:activity-ktx"], "1.2.0"),
    ("core", ["androidx.core:core", "androidx.core:core-ktx"], "1.6.0"),
    ("appcompat", ["androidx.appcompat:appcompat"], "1.3.0"),
    ("lifecycle", ["androidx.lifecycle:lifecycle"], "2.3.0"),
]

_COORDS_MAP = {
    "fragment": "androidx.fragment:fragment-ktx",
    "activity": "androidx.activity:activity-ktx",
    "core": "androidx.core:core-ktx",
    "appcompat": "androidx.appcompat:appcompat",
    "lifecycle": "androidx.lifecycle:lifecycle-runtime-ktx",
}


def run(cmd, cwd=None, timeout=None, env=None):
    return subprocess.run(cmd, shell=isinstance(cmd, str), cwd=cwd,
                          capture_output=True, text=True,
                          timeout=timeout, env=env)


# ------------------------------------------------------------------ lint ----
def parse_lint_upgrade_requests(log_text: str) -> list:
    pattern = re.compile(
        r"Upgrade\s+(\w+)\s+version\s+to\s+at\s+least\s+(\d+\.\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    seen, out = set(), []
    for m in pattern.finditer(log_text):
        k = (m.group(1).lower(), m.group(2))
        if k not in seen:
            seen.add(k)
            out.append({"keyword": m.group(1).lower(), "requested_version": m.group(2)})
    return out


def module_build_file(root: Path, module: str) -> Path | None:
    md = root / module
    for name in ("build.gradle.kts", "build.gradle"):
        p = md / name
        if p.is_file():
            return p
    return None


def bump_dep(bf: Path, keyword: str, requested_version: str) -> dict:
    text = bf.read_text(errors="ignore")
    hints = [(subs, fb) for kw, subs, fb in _LINT_HINTS if kw == keyword]
    if not hints:
        hints = [([keyword], requested_version)]
    new_text, bumped, changes = text, False, []
    for substrings, fallback_ver in hints:
        for sub in substrings:
            ver_to_use = requested_version or fallback_ver
            pat = re.compile(
                re.escape(sub) + r"\s*:\s*([\"'])([0-9]+(?:\.[0-9]+)*(?:[-+][\w.\-]+)?)\1"
            )
            def repl(m):
                nonlocal bumped
                old = m.group(2)
                if old == ver_to_use:
                    return m.group(0)
                bumped = True
                changes.append(f"{sub}: {old} -> {ver_to_use}")
                return f"{sub}:{m.group(1)}{ver_to_use}{m.group(1)}"
            new_text = pat.sub(repl, new_text)
    if bumped:
        bf.write_text(new_text)
    return {"bumped": bumped, "changes": changes}


def maybe_add_dep(bf: Path, keyword: str, requested_version: str) -> dict:
    text = bf.read_text(errors="ignore")
    if keyword in text:
        return {"added": False, "reason": "already present"}
    m = re.search(r"dependencies\s*\{", text)
    if not m:
        return {"added": False, "reason": "no dependencies block"}
    coords = _COORDS_MAP.get(keyword, keyword)
    new_line = f'\nimplementation("{coords}:{requested_version}")\n'
    bf.write_text(text[:m.end()] + new_line + text[m.end():])
    return {"added": True, "added_line": new_line.strip()}


def apply_lint_fixes(root: Path, module: str, log_text: str) -> list:
    requests = parse_lint_upgrade_requests(log_text)
    if not requests:
        return []
    bf = module_build_file(root, module)
    if bf is None:
        return [{"error": "no build file for module"}]
    actions = []
    for r in requests:
        kw, ver = r["keyword"], r["requested_version"]
        print(f"[lint-fix] bumping '{kw}' to '{ver}'…", flush=True)
        bump = bump_dep(bf, kw, ver)
        actions.append({"action": "bump", **bump})
        if not bump["bumped"]:
            actions.append({"action": "add_if_missing", **maybe_add_dep(bf, kw, ver)})
    return actions


# ----------------------------------------------------------------- build ----
def run_build_once(root: Path, module: str, task: str, attempt: int,
                   env_base: dict, logs_dir: Path) -> tuple:
    gradlew = root / "gradlew"
    full_task = f"{module}:{task}" if module and not task.startswith(":") else task
    cmd = [str(gradlew), "--no-daemon", "--stacktrace",
           "-Pandroid.suppressUnsupportedCompileSdk=34,35,36", full_task]
    log_path = logs_dir / f"build_attempt{attempt}_{int(time.time())}.log"
    print(f"[build] attempt={attempt} cmd={' '.join(cmd)}", flush=True)
    buf = []
    with open(log_path, "w") as logf:
        proc = subprocess.Popen(cmd, cwd=root, env=env_base,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            logf.write(line)
            buf.append(line)
            if any(s in line for s in ("FAILED", "BUILD SUCCESSFUL", "BUILD FAILED",
                                       "> Task :", "FAILURE:")):
                print("   " + line.rstrip()[:200], flush=True)
        rc = proc.wait()
    return rc, buf, log_path


def run_main_build(root: Path, module: str, task: str, logs_dir: Path) -> tuple:
    env_base = os.environ.copy()
    env_base["ANDROID_SDK_ROOT"] = os.environ.get("ANDROID_SDK_ROOT",
                                                  "/usr/local/lib/android/sdk")
    env_base["ANDROID_HOME"] = env_base["ANDROID_SDK_ROOT"]
    attempts_log = []
    for attempt in range(1, MAX_RETRIES + 1):
        rc, buf, log_path = run_build_once(root, module, task, attempt, env_base, logs_dir)
        attempts_log.append({"attempt": attempt, "rc": rc, "log": str(log_path)})
        if rc == 0:
            return True, attempts_log
        joined = "".join(buf)
        reqs = parse_lint_upgrade_requests(joined)
        if reqs:
            print(f"[build] attempt {attempt} falló; lint pide bumps: {reqs}", flush=True)
            acts = apply_lint_fixes(root, module, joined)
            attempts_log[-1]["actions_taken"] = acts
            if attempt < MAX_RETRIES:
                wait = BACKOFF * (2 ** (attempt - 1))
                print(f"[build] reintento en {wait}s…", flush=True)
                time.sleep(wait)
                continue
            break
        err_kind = f"rc={rc}"
        for needle, kind in [("SDK location not found", "SDK missing"),
                             ("Could not resolve", "dependency resolution"),
                             ("Unresolved reference", "kotlin unresolved"),
                             ("Keystore file", "keystore missing")]:
            if needle in joined:
                err_kind = kind
                break
        attempts_log[-1]["err_kind"] = err_kind
        print(f"[build] FAILED attempt {attempt} ({err_kind}). Últimas 20 líneas:", flush=True)
        for line in buf[-20:]:
            print("   " + line.rstrip()[:240], flush=True)
        break
    return False, attempts_log


# ------------------------------------------------------------------ sign ----
def find_build_tools(sdk_root: Path) -> Path:
    bt = sdk_root / "build-tools"
    versions = sorted((d for d in bt.iterdir() if d.is_dir()),
                      key=lambda d: [int(x) for x in re.findall(r"\d+", d.name)[:3]],
                      reverse=True) if bt.is_dir() else []
    if not versions:
        raise SystemExit("[sign] no hay build-tools en el SDK")
    return versions[0]


def sign_artifacts(root: Path, module: str, ks: Path, ks_pass: str,
                   ks_alias: str, key_pass: str) -> list:
    sdk_root = Path(os.environ.get("ANDROID_SDK_ROOT", "/usr/local/lib/android/sdk"))
    bt_dir = find_build_tools(sdk_root)
    zipalign, apksigner = bt_dir / "zipalign", bt_dir / "apksigner"
    out_dir = root / module / "build" / "outputs" / "apk" / "release"
    apks = sorted(out_dir.glob("*.apk")) if out_dir.is_dir() else []
    if not apks:
        # búsqueda de respaldo en todo el módulo
        apks = sorted((root / module).rglob("*.apk"))
    print(f"[sign] build-tools: {bt_dir.name} · alias={ks_alias} · APKs={len(apks)}", flush=True)
    signed = []
    for art in apks:
        final_name = art.name.replace("unsigned", "signed")
        if final_name == art.name:
            final_name = art.stem + ".signed" + art.suffix
        target = art.parent / final_name
        temp = art.with_suffix(".signed.tmp")
        r1 = run([str(zipalign), "-p", "4", str(art), str(temp)])
        if r1.returncode != 0:
            print(f"[sign] zipalign FAIL {art.name}: {r1.stderr[:300]}", flush=True)
            signed.append({"artifact": str(art), "signed": False})
            continue
        r2 = run([str(apksigner), "sign", "--ks", str(ks), f"--ks-pass", f"pass:{ks_pass}",
                  "--ks-key-alias", ks_alias, "--key-pass", f"pass:{key_pass}",
                  "--out", str(temp), str(temp)])
        if r2.returncode != 0:
            print(f"[sign] apksigner FAIL {art.name}: {r2.stderr[:300]}", flush=True)
            signed.append({"artifact": str(art), "signed": False})
            continue
        shutil.move(str(temp), str(target))
        ver = run([str(apksigner), "verify", "--print-certs", str(target)])
        print(f"[sign] OK {target.name} (verify rc={ver.returncode})", flush=True)
        signed.append({"artifact": str(target), "signed": True, "verify_rc": ver.returncode,
                       "size_mb": round(target.stat().st_size / 1_048_576, 1)})
    return signed


def sanitize(text: str, secrets: list) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-root", required=True)
    ap.add_argument("--module", default="app")
    ap.add_argument("--task", default="assembleRelease")
    ap.add_argument("--keystore", required=True)
    ap.add_argument("--ks-pass", required=True)
    ap.add_argument("--ks-alias", required=True)
    ap.add_argument("--key-pass", default=None, help="si difiere del keystore pass")
    ap.add_argument("--out-dir", default="artifacts")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    root = Path(args.project_root).resolve()
    out_dir = Path(args.out_dir).resolve()
    logs_dir = root.parent / "build-logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    gradlew = root / "gradlew"
    if not gradlew.is_file():
        raise SystemExit("[build] el repo no trae gradlew")
    gradlew.chmod(gradlew.stat().st_mode | 0o755)

    ok, attempts = run_main_build(root, args.module, args.task, logs_dir)
    if not ok:
        (out_dir / "build_report.json").write_text(json.dumps(
            {"ok": False, "attempts": sanitize(json.dumps(attempts), [args.ks_pass])},
            indent=2, ensure_ascii=False), encoding="utf-8")
        print("[build] COMPILACIÓN FALLIDA tras los intentos", flush=True)
        return 1

    print("[build] COMPILACIÓN EXITOSA ✓", flush=True)

    key_pass = args.key_pass or args.ks_pass
    signed = sign_artifacts(root, args.module, Path(args.keystore),
                            args.ks_pass, args.ks_alias, key_pass)
    good = [s for s in signed if s.get("signed")]
    if not good:
        print("[sign] no se firmó ningún APK", flush=True)
        return 2

    tag = args.tag or "manual"
    final_paths = []
    for s in good:
        src = Path(s["artifact"])
        name = f"DictateKeyboard-{tag}-{src.name if len(good) == 1 else src.name}"
        dst = out_dir / name
        shutil.copy2(src, dst)
        final_paths.append(str(dst))
        print(f"[export] {dst.name} ({s.get('size_mb')} MB)", flush=True)

    (out_dir / "build_report.json").write_text(json.dumps(
        {"ok": True, "attempts": sanitize(json.dumps(attempts), [args.ks_pass]),
         "signed": signed, "delivered": final_paths},
        indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"ok": True, "apks": final_paths}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
