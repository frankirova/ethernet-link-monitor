# Dashboard Textual

> Un tablero en la terminal que muestra el estado real de cada enlace.

`dashboard.py` es la vista del monitor: se conecta al monitor que ya está corriendo (o lo arranca si no hay ninguno) y dibuja su estado con [Textual](https://textual.textualize.io/) y Rich. **Cerrar el dashboard no detiene el monitoreo** ni las alertas de Telegram; es solo una pantalla.

## Uso

```bash
# Con dependencias instaladas (ver README, paso 1)
.venv/bin/python dashboard.py --demo        # monitor demo propio, sin red ni Telegram
.venv/bin/python dashboard.py               # se conecta al monitor real o lo arranca
.venv/bin/python dashboard.py --once        # imprime un snapshot y sale
```

Sin terminal interactiva (`--once`) imprime texto plano; sin `--once` y sin TTY sale con código 2 y el mensaje `dashboard needs a terminal`.

## Teclas

| Tecla | Acción |
| --- | --- |
| `q` / `Ctrl+C` | Cierra el dashboard. El monitor sigue vivo. |
| `s` | Detiene el monitor (equivale a `monitor.py --stop`). Deja de mandar Telegram hasta que lo vuelvas a arrancar. |
| `r` | Fuerza un refresco inmediato. |
| `o` | Alterna orden: primero los enlaces peores, o el orden del inventario. |
| mouse / flechas | Selecciona una fila y muestra su detalle en el panel derecho. |

## Qué muestra

```
┌ Ethernet link monitor ──────────────────────────────── 14:32:44 ─┐
│ DEMO  DOWN 1  LOW 1  UNREACHABLE 1  UNKNOWN 1  OK 1              │
│ Scans 1  |  last 14:32:44  |  pid 3313551                        │
├───────────────────────────────┬──────────────────────────────────┤
│ Status   Host      IP     ... │ Demo link 3                      │
│ DOWN     Demo link 3  ...     │ DOWN  - / min 1.0G               │
│ LOW      Demo link 2  ...     │ 192.0.2.3  port 1                │
│ ...                           │ Synthetic data; no network ...    │
│                               ├──────────────────────────────────┤
│                               │ ALERT: Demo link 3 ...           │
├───────────────────────────────┴──────────────────────────────────┤
│ q Quit   r Refresh   s Stop monitor   o Sort                     │
└──────────────────────────────────────────────────────────────────┘
```

- **Tabla**: estado, nombre, IP, puerto (`ifindex`), velocidad negociada y mínimo configurado. Los enlaces peores van primero.
- **Resumen**: cuántos hosts hay en cada estado y con cuántos escaneos completados viene el dato.
- **Detalle**: la fila seleccionada, con el motivo del estado (`detail`).
- **Alertas**: los últimos avisos que publicó el monitor (los mismos que van por Telegram).

## Estados de la pantalla

El encabezado muestra siempre el modo (`LIVE`/`DEMO`) y le suma el estado de salud del dato cuando no es normal (`STARTING`, `STALE`, `DISCONNECTED`, `OUTDATED`), para que un monitor demo, caído o viejo no parezca sano.

| Estado | Significado |
| --- | --- |
| `LIVE` | El monitor está publicando escaneos frescos. |
| `DEMO` | El monitor conectado es el demo: datos sintéticos, sin red ni Telegram. |
| `STARTING` | El monitor está vivo pero todavía no completó el primer escaneo. |
| `STALE` | El último escaneo completo es más viejo que el intervalo del monitor + 20 s (mínimo 30 s). No implica enlace sano. |
| `DISCONNECTED` | No hay monitor corriendo con esa identidad; la tabla se vacía en vez de mostrar datos viejos. |
| `OUTDATED` | El monitor corriendo es de una versión anterior y publica solo texto: no hay filas por host. Se detiene con `s` y se vuelve a arrancar. |

## Cómo se conecta

```
dashboard.py ──► monitor.sock (Unix, 0600, dueño-only) ──► monitor en background
```

1. Resuelve el directorio privado de runtime (`--runtime-dir` o `$XDG_RUNTIME_DIR/ethernet-monitor`).
2. Si hay un monitor corriendo, se adjunta **sin validar ni cambiar** su configuración: el badge `LIVE`/`DEMO` sale de la identidad que publica el propio monitor, no de los flags de este comando.
3. Si no hay ninguno, lo arranca con sus `--hosts`, `--demo` e `--interval` (`--demo` usa 3 s para que se vea rotar).
4. Cada 0,5 s pide el snapshot en un worker aparte: la interfaz no se bloquea esperando el socket.

El snapshot del monitor incluye `records` (un registro por host: nombre, IP, puerto, velocidad, mínimo, estado y detalle) y `notices` (últimos avisos), además de `lines` que sigue usando el visor curses de `monitor.py`.

## Por qué se reemplazó el anterior

El dashboard anterior leía `hosts.csv` y dibujaba un frame estático: los datos solo se llenaban en modo demo y `stdscr.getch()` bloqueaba sin `nodelay`, así que la pantalla no se refrescaba nunca y no tenía relación con el monitor real. La versión Textual muestra el mismo dato que produce el monitor, con refresco automático y sin poder matar el proceso por accidente.

## Requisitos

`textual` y `rich` están en `requirements.txt`. Si falta `textual`, `dashboard.py` sale con código 2 y el comando exacto de instalación (lo mismo hace `run_dashboard.sh`), en vez de un traceback de import. Si preferís cero dependencias, el visor curses de `monitor.py` sigue disponible y no cambia.

### Después de actualizar el código

Un monitor que ya estaba corriendo sigue con el código viejo en memoria: si arrancó antes de este dashboard, no publica filas por host y la pantalla lo marca `OUTDATED`. Detenelo con `s` (o `monitor.py --stop`) y volvé a arrancarlo para que publique `records`.

## Verificación

```bash
.venv/bin/python -m unittest discover -s tests -v
```

`tests/test_dashboard.py` cubre los helpers (formato, resumen, orden, detección de `STALE`/`DISCONNECTED`, badge tomado de la identidad del monitor), el refresco sin bloquear la interfaz, frames renderizados con el *pilot* de Textual, y el camino real en terminal: `--once` arranca y detiene su propio monitor, adjuntarse no detiene el ajeno, `q` y `s` se comportan distinto, y el motivo real aparece cuando el monitor no puede arrancar.
