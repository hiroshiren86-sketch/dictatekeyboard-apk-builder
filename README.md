# DictateKeyboard APK Builder

Compilación y firma **automática** de la APK de [DevEmperor/DictateKeyboard](https://github.com/DevEmperor/DictateKeyboard) cada vez que el desarrollador publica un tag nuevo. Sin Colab, sin Google Drive, sin pasos manuales: 100% GitHub Actions.

El desarrollador solo publica el código fuente (la APK de Play Store es de pago), así que este repo la compila y firma con el mismo keystore que se usaba en el cuaderno de Colab → **las actualizaciones se instalan por encima de las APKs anteriores sin desinstalar nada**.

## Cómo funciona

```
DevEmperor publica tag (ej. v6.1.0)
        │
        ▼
┌─────────────────────────────────────────────┐
│ monitor.yml  (cron cada 15 min: :03 :18 :33 :48) │
│  tag_monitor.py check                       │
│   · git ls-remote --tags + orden semver     │
│   · ¿tag nuevo? → commit de estado +        │
│     gh workflow run build.yml               │
└─────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────┐
│ build.yml  (motor Gradle nativo)            │
│  · checkout del fuente en el SHA del tag    │
│  · JDK 17 + Android SDK del runner          │
│  · patch_project.py   (Celda 6 del cuaderno)│
│      - bump fragment 1.3.0                  │
│      - sherpa-onnx (tools/fetch-sherpa-onnx)│
│  · build_and_sign.py  (Celdas 9-10)         │
│      - gradlew assembleRelease (4 reintentos│
│        + auto-fix de lint)                  │
│      - zipalign + apksigner (build-tools 36)│
│  · Release con la APK + artifact (30 días)  │
│  · Notificación a Discord (éxito/fallo)     │
│  · tag_monitor.py finish (reintentos)       │
└─────────────────────────────────────────────┘
```

**Reintentos**: si un build falla, el monitor lo reencola automáticamente hasta 2 veces más (`max_retries` en `state/state.json`).

## Secrets requeridos

| Secret | Contenido |
|---|---|
| `KEYSTORE_B64` | Keystore de firma en base64 |
| `KEYSTORE_PASSWORD` | Contraseña del keystore (store y key) |
| `KEYSTORE_ALIAS` | Alias de la clave (`dictatekeyboard`) |
| `DISCORD_WEBHOOK_URL` | *Opcional* — webhook de Discord para avisos |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | *Opcional* — aviso por Telegram (chat privado con un bot) |

Ningún material sensible vive en el código: el keystore y sus contraseñas están solo en los **Secrets cifrados de GitHub**. Este repo es público a propósito: en repos públicos Actions es gratis e ilimitado; en uno privado, el cron de 15 min (≈2.900 min/mes) agotaría los 2.000 minutos gratis del plan y la vigilancia moriría a mitad de mes.

## Uso manual

- **Forzar un build de un tag**: *Actions → build-apk → Run workflow* → escribe el tag (vacío = el mayor semver actual).
- **Probar el monitor**: *Actions → monitor-latest-tag → Run workflow*.
- **Ver el estado**: `scripts/tag_monitor.py show` o mira `state/state.json`.

## Diagnóstico rápido

| Síntoma | Causa probable |
|---|---|
| Build omitido con aviso de secrets | Faltan `KEYSTORE_B64` / `KEYSTORE_PASSWORD` / `KEYSTORE_ALIAS` |
| Falla en parche sherpa-onnx | Sin conexión a GitHub releases del script `tools/fetch-sherpa-onnx.sh` (reintenta el run) |
| `finish` marca failure 3 veces | Revisa el log del paso *Compilar + firmar*; los 4 intentos de Gradle dejan la pista completa |
| No llega aviso a Discord | `DISCORD_WEBHOOK_URL` vacío o inválido (el build sigue funcionando igual) |

## Estructura

```
.github/workflows/
  monitor.yml        vigilante cron 15 min → detecta tag nuevo
  build.yml          constructor Gradle + firma + Release + Discord
scripts/
  tag_monitor.py     detección semver (réplica Celda 5 del cuaderno)
  patch_project.py   parches de compilación (réplica Celda 6)
  build_and_sign.py  build con reintentos + zipalign/apksigner (Celdas 9-10)
state/state.json     estado persistido (último tag, reintentos, historial)
patches/             parches de usuario opcionales (patch -p1 --forward)
```
