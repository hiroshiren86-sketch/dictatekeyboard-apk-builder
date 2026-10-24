#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_native_libs.py — Compila las librerías nativas que el proyecto fuente
ya NO trae precompiladas pero exige en tiempo de build.

Caso conocido (upstream ≥ v6.4.0, issue #424): `app/build.gradle.kts` registra
el task `verifyBergamotLib`, que exige
`app/src/main/jniLibs/arm64-v8a/libdictate_bergamot.so`. Esa librería NO viene
en el repo upstream: se compila con `tools/bergamot/build-android.sh`
(Mozilla Bergamot + Marian fork + NDK). Este script automatiza eso:

  1. Si la .so ya existe            → no-op (build normal continúa).
  2. Si no existe y el proyecto trae tools/bergamot/build-android.sh:
       - lee NDK/CMake exigidos en gradle/tools.versions.toml
       - los instala con sdkmanager si faltan en el runner
       - ejecuta el build oficial de upstream (ABI arm64-v8a)
  3. Sale con error claro si algo no se puede satisfacer.

Diseñado para correr en el workflow build.yml ANTES de "Compilar + firmar".
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REQUIRED_SO = "app/src/main/jniLibs/arm64-v8a/libdictate_bergamot.so"
BERGAMOT_SCRIPT = "tools/bergamot/build-android.sh"
BUILD_TIMEOUT_SECS = 90 * 60  # margen holgado; upstream tarda "unos minutos"


def log(msg: str) -> None:
    print(f"[native] {msg}", flush=True)


def stream_run(cmd: list, cwd: Path, env: dict, timeout: int, label: str) -> int:
    """Ejecuta un comando haciendo streaming de su salida a stdout."""
    log(f"ejecutando: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    assert proc.stdout is not None
    start = time.time()
    for line in proc.stdout:
        print(f"[{label}] {line.rstrip()[:240]}", flush=True)
    rc = proc.wait()
    log(f"{label} terminó rc={rc} ({int(time.time() - start)}s)")
    return rc


def parse_required_versions(root: Path) -> tuple[str, str]:
    """Extrae ndk y cmake de gradle/tools.versions.toml del proyecto fuente."""
    toml_path = root / "gradle" / "tools.versions.toml"
    if not toml_path.is_file():
        raise SystemExit("[native] no existe gradle/tools.versions.toml; no sé qué NDK/CMake instalar")
    text = toml_path.read_text(encoding="utf-8", errors="ignore")
    m_ndk = re.search(r'^ndk\s*=\s*"([^"]+)"', text, re.M)
    m_cmake = re.search(r'^cmake\s*=\s*"([^"]+)"', text, re.M)
    if not m_ndk or not m_cmake:
        raise SystemExit("[native] no pude leer las versiones de ndk/cmake en tools.versions.toml")
    return m_ndk.group(1), m_cmake.group(1)


def sdk_root() -> Path:
    return Path(os.environ.get("ANDROID_HOME")
                or os.environ.get("ANDROID_SDK_ROOT")
                or "/usr/local/lib/android/sdk")


def find_sdkmanager(sdk: Path) -> Path | None:
    cand = sdk / "cmdline-tools" / "latest" / "bin" / "sdkmanager"
    if cand.is_file():
        return cand
    # respaldo: cualquier cmdline-tools/*/bin/sdkmanager
    ct = sdk / "cmdline-tools"
    if ct.is_dir():
        for d in sorted(ct.iterdir(), reverse=True):
            p = d / "bin" / "sdkmanager"
            if p.is_file():
                return p
    return None


def ensure_sdk_components(sdk: Path, ndk_ver: str, cmake_ver: str) -> list:
    """Instala con sdkmanager los componentes que falten. Devuelve lo instalado."""
    needed = []
    ndk_dir = sdk / "ndk" / ndk_ver
    if not ndk_dir.is_dir():
        needed.append(f"ndk;{ndk_ver}")
    cmake_bin = sdk / "cmake" / cmake_ver / "bin" / "cmake"
    ninja_bin = sdk / "cmake" / cmake_ver / "bin" / "ninja"
    if not (cmake_bin.is_file() and ninja_bin.is_file()):
        needed.append(f"cmake;{cmake_ver}")

    if not needed:
        log(f"NDK {ndk_ver} y CMake {cmake_ver} ya presentes en el runner")
        return []

    sdkmanager = find_sdkmanager(sdk)
    if sdkmanager is None:
        raise SystemExit(
            f"[native] faltan {needed} y no encontré sdkmanager en {sdk}; "
            "no puedo instalar las herramientas nativas")
    env = os.environ.copy()
    env.setdefault("ANDROID_HOME", str(sdk))
    env.setdefault("ANDROID_SDK_ROOT", str(sdk))
    log(f"instalando con sdkmanager: {needed} (puede tardar varios minutos)…")
    rc = stream_run([str(sdkmanager), "--install", *needed],
                    cwd=Path.cwd(), env=env, timeout=25 * 60, label="sdkmanager")
    if rc != 0:
        raise SystemExit(f"[native] sdkmanager falló (rc={rc}) instalando {needed}")
    # re-verificación dura
    for p in ([ndk_dir] if "ndk;" in "".join(needed) else []) + \
             ([cmake_bin, ninja_bin] if "cmake;" in "".join(needed) else []):
        if not (p.is_dir() or p.is_file()):
            raise SystemExit(f"[native] sdkmanager terminó pero falta: {p}")
    return needed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-root", required=True)
    args = ap.parse_args()
    root = Path(args.project_root).resolve()

    so_path = root / REQUIRED_SO
    bergamot = root / BERGAMOT_SCRIPT

    if so_path.is_file():
        log(f"{REQUIRED_SO} ya existe ({so_path.stat().st_size // 1024} KiB); nada que hacer")
        print(json.dumps({"ok": True, "action": "skip", "reason": "so ya presente"}))
        return 0

    if not bergamot.is_file():
        log(f"el proyecto no trae {BERGAMOT_SCRIPT}; no se requieren libs nativas extra")
        print(json.dumps({"ok": True, "action": "skip", "reason": "sin script bergamot"}))
        return 0

    # ¿El build de Gradle realmente exige la librería? (task verifyBergamotLib)
    app_gradle = root / "app" / "build.gradle.kts"
    needs_it = app_gradle.is_file() and "verifyBergamotLib" in app_gradle.read_text(errors="ignore")
    if not needs_it:
        # igual intentamos: si el script existe, upstream espera que se use.
        log("no vi el task verifyBergamotLib, pero el script existe → compilo igual por seguridad")

    ndk_ver, cmake_ver = parse_required_versions(root)
    log(f"upstream exige NDK={ndk_ver} · CMake={cmake_ver}")
    sdk = sdk_root()
    installed = ensure_sdk_components(sdk, ndk_ver, cmake_ver)

    bergamot.chmod(bergamot.stat().st_mode | 0o755)
    env = os.environ.copy()
    env["ANDROID_HOME"] = str(sdk)
    env["ANDROID_SDK_ROOT"] = str(sdk)
    env.setdefault("CMAKE_POLICY_VERSION_MINIMUM", "3.5")

    rc = stream_run(["bash", BERGAMOT_SCRIPT, "arm64-v8a"],
                    cwd=root, env=env, timeout=BUILD_TIMEOUT_SECS, label="bergamot")
    if rc != 0:
        print(json.dumps({"ok": False, "action": "build", "rc": rc,
                          "installed": installed}), flush=True)
        raise SystemExit("[native] tools/bergamot/build-android.sh falló; revisa el log de arriba")

    if not so_path.is_file():
        raise SystemExit(f"[native] el build terminó pero NO generó {REQUIRED_SO}")

    size_mb = round(so_path.stat().st_size / 1_048_576, 1)
    log(f"libdictate_bergamot.so generada ({size_mb} MB) ✓")
    print(json.dumps({"ok": True, "action": "built", "abi": "arm64-v8a",
                      "size_mb": size_mb, "installed": installed}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
