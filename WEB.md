# Panel web local

> Una página en el navegador que muestra el mismo estado que el monitor, sin agregar dependencias.

`web.py` es otra vista del monitor: se conecta al que ya está corriendo (o lo arranca si no hay ninguno) y dibuja su estado en una página local. **Cerrar la página, el proceso o Ctrl+C no detiene el monitoreo** ni las alertas de Telegram; es solo una pantalla. Tiene el mismo contrato que `dashboard.py` y lee el mismo snapshot que el visor de la terminal.

## Uso

```bash
python3 web.py --demo      # datos sintéticos, sin red, credenciales ni Telegram
python3 web.py             # se conecta al monitor real o lo arranca
python3 web.py --port 0    # elige un puerto libre e imprime cuál
```

`--demo` funciona sin el `.venv`: `web.py` usa solo la biblioteca estándar y no necesita `textual`, `rich` ni `pysnmp`. El monitoreo real sí necesita las dependencias de `requirements.txt`, porque el monitor es el que consulta por SNMP; el panel no agrega ninguna.

Al arrancar imprime la URL que tomó y un recordatorio:

```text
Ethernet monitor web dashboard: http://127.0.0.1:8765/
A viewer only: closing it never stops monitoring. Ctrl+C to close.
```

Si no puede arrancar el monitor, avisa por `stderr` y sirve la página igual: la pantalla marca `DISCONNECTED` en vez de esconderlo. Si el puerto está ocupado, sale con código 2 y el mensaje `error: port <PORT> is not available; choose another with --port`.

### Opciones

Salida exacta de `python3 web.py --help`:

```text
usage: web.py [-h] [--demo] [--hosts HOSTS] [--interval INTERVAL]
              [--runtime-dir RUNTIME_DIR] [--port PORT]

Local web dashboard for the Ethernet link monitor.

options:
  -h, --help            show this help message and exit
  --demo                synthetic data; no network, credentials or Telegram
  --hosts HOSTS         editable host CSV (default: hosts.csv)
  --interval INTERVAL   scan interval used when this dashboard starts the
                        monitor (default: 60)
  --runtime-dir RUNTIME_DIR
                        private directory for local monitor IPC
  --port PORT           loopback port to listen on (default: 8765); 0 picks a
                        free port

It is a viewer: closing it never stops monitoring. It binds 127.0.0.1 only, so
reach it from another machine with an SSH tunnel, not by widening the bind.
```

- `--hosts` y `--interval` se usan solo cuando el panel tiene que arrancar el monitor. Si ya hay uno corriendo, se adjunta sin validar ni cambiar su configuración.
- `--demo` con el intervalo por defecto (60) usa 3 segundos, para que se vea rotar.
- `--runtime-dir` elige el directorio privado de IPC; usá uno distinto para una instancia distinta.
- `--port` acepta 0 para pedirle un puerto libre al sistema operativo. El rango válido es 0–65535; `--interval` tiene que ser positivo y finito.

## Qué muestra

De arriba abajo, en este orden:

1. **Encabezado**: la palabra `enlaces`, el badge con el modo del monitor y, cuando el dato no es fresco, la frescura al lado, la hora del último escaneo completado y su antigüedad.
2. **Veredicto**: una frase con el estado del conjunto.
3. **Desglose**: cuántos enlaces hay en cada estado.
4. **Nota**: aparece solo cuando hay algo que explicar (monitor ausente, dato viejo, monitor viejo, primer escaneo).
5. **Filas**: una por enlace, con LED, nombre, `ip:ifindex`, velocidad negociada, mínimo configurado y la palabra de estado. Los peores van primero.
6. **Último aviso**: el último mensaje del monitor, tal cual, sin reformatear.
7. **Pie**: archivo de datos, cantidad de escaneos, intervalo y pid.

El panel nunca reporta como sano un enlace que nadie pudo leer. `DOWN` y `LOW` cuentan como fuera de norma; `UNREACHABLE`, `UNKNOWN` y `PENDING` cuentan como sin dato; solo `OK` cuenta como sano. Las palabras de estado se muestran tal cual porque son las mismas que usan Telegram y los dashboards de terminal.

### El LED

El LED es la metáfora, y el color nunca es la única señal: la palabra de estado siempre está al lado.

| LED | Estado | Qué significa |
| --- | --- | --- |
| Verde encendido | `OK` | Enlace gigabit sano. |
| Ámbar encendido | `LOW` | Enlace a 100 Mbps: diferencia real de velocidad negociada. |
| Zócalo oscuro, apagado | `DOWN` | El puerto está caído. |
| Anillo hueco | `UNREACHABLE`, `UNKNOWN`, `PENDING` | El zócalo está, la luz no llegó: no hay dato. |

## Estados de la pantalla

El badge informa dos cosas: el modo (`LIVE` o `DEMO`, según la identidad que publica el monitor) y, cuando el dato no es fresco, el estado al lado (`LIVE STALE`, `DEMO STARTING`). Con el monitor caído no hay modo que informar y el badge dice solamente `DISCONNECTED`. La frase del veredicto y la nota explican el resto.

| Estado | Significado |
| --- | --- |
| `LIVE` | El monitor conectado no es el demo. |
| `DEMO` | El monitor conectado es el demo: datos sintéticos, sin red y sin Telegram. La página lo dice con todas las letras. |
| `STARTING` | El monitor está vivo pero todavía no completó ningún escaneo. Se suma al badge. |
| `STALE` | El último escaneo completo es más viejo que el intervalo del monitor + 20 s (mínimo 30 s). No implica enlaces sanos. Se suma al badge. |
| `OUTDATED` | El monitor corriendo es de una versión anterior y publica solo texto: hay que pararlo y arrancarlo de nuevo para que publique filas por enlace. Se suma al badge. |
| `DISCONNECTED` | No hay monitor corriendo. El badge muestra solo esa palabra y la lista se vacía en vez de mostrar datos viejos. |

Cuando el servidor local deja de responder, el badge deja de repetir el último estado del monitor y pasa a `SIN SERVIDOR`, la frase dice “No se pudo leer el panel” y la página sigue reintentando cada 2 segundos. Cuando el dato no es confiable, la lista se atenúa y se desatura: el color no puede jurar nada sobre un snapshot viejo.

## Detalle por enlace

Al hacer clic, tocar o activar una fila con el teclado se despliega un detalle en línea con: estado, velocidad, mínimo, puerto, ip y motivo. El detalle usa `aria-expanded`/`aria-controls` y la fila queda marcada mientras está abierta. La lista se reconstruye solo cuando cambia el dato de algún enlace, así que la fila que estás leyendo conserva el foco y el detalle abierto entre refrescos.

## Cómo se conecta

```text
navegador ──► web.py (127.0.0.1:8765) ──► monitor.sock (Unix, 0600) ──► monitor en background
```

1. Resuelve el directorio privado de runtime (`--runtime-dir` o `$XDG_RUNTIME_DIR/ethernet-monitor`).
2. Si hay un monitor corriendo, se adjunta sin cambiar su configuración. El modo del badge (`LIVE`/`DEMO`) sale de la identidad que publica el propio monitor, no de los flags de este comando.
3. Si no hay ninguno, lo arranca con sus `--hosts`, `--demo` e `--interval`.
4. La página pide `GET /api/snapshot` cada 2 segundos y pausa el pedido mientras la pestaña está oculta.

La superficie HTTP es la página más un único endpoint de solo lectura, `GET /api/snapshot`. Solo se responde GET: cualquier otro método, `OPTIONS` y `TRACE` incluidos, se rechaza con 405 y `Allow: GET`, y el rechazo va sin cuerpo. Sirve cuatro assets explícitos, nunca lista un directorio y nunca sirve un archivo por nombre pedido. Cada respuesta, rechazos incluidos, lleva `Cache-Control: no-store` y una Content-Security-Policy que no permite ningún origen externo. El snapshot expone el nombre del archivo de hosts, nunca su ruta absoluta.

## Límites

- **Solo loopback.** Escucha en `127.0.0.1` y no hay opción para ampliar el bind.
- **Sin autenticación.** Cualquier proceso de esta máquina puede leer el snapshot. No lo expongas en una interfaz de red.
- **Sin historial, sin gráficos, sin base de datos.** El monitor no guarda historia, así que el panel no la inventa. Un snapshot viejo se muestra como viejo, no se interpola.
- **Un solo monitor por lista de hosts.** Igual que el resto de las vistas: correr dos monitores sobre la misma lista duplica notificaciones. Un panel arrancado con otro `--hosts` o `--runtime-dir` se conecta a otro monitor o arranca uno segundo.
- **No puede detener el monitor.** La página solo lee; no hay botón de stop ni endpoint de escritura. Para parar, usá `monitor.py --stop` (o `s` en `dashboard.py`).
- **Sin tema manual.** Sigue la preferencia clara/oscura del sistema operativo; no hay selector.
- **Sin red externa.** No carga CDN, fuentes ni íconos remotos, así que funciona en un sitio sin internet. Tampoco es un panel remoto: para verlo desde otra máquina, túnel.
- **El proceso no es un servicio.** Cerrar el panel no detiene el monitoreo, pero apagar la máquina o matar el monitor sí. No se instala ninguna política de reinicio.

## Cómo verlo desde otra máquina

No amplíes el bind. Abrí un túnel SSH contra el host que corre el monitor:

```bash
ssh -L 8765:127.0.0.1:8765 user@monitor-host
```

Y en tu máquina abrí `http://127.0.0.1:8765/`. El puerto del túnel y el de `--port` tienen que coincidir; si usaste otro puerto local, cambiá el primer número.

## Qué no hace

No manda Telegram, no confirma transiciones, no reintenta entregas, no escribe archivos y no modifica `hosts.csv`. Es una vista: toda la lógica de estado y de alertas vive en el monitor.

## Verificación

```bash
.venv/bin/python -m unittest discover -s tests -v
```

`tests/test_web.py` cubre la clasificación del payload (peores primero, `tone`, veredicto, nombre de archivo en vez de ruta absoluta), el guardia de rutas absolutas, la superficie HTTP (200 en la página y en `/api/snapshot`, 404 en lo desconocido, 405 con `Allow: GET`, `no-store` y CSP en todos los verbos que no son GET —`POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, `OPTIONS`, `TRACE`, `CONNECT` y uno inventado—, con cuerpo vacío), que el servidor escuche solo en loopback y rechace un puerto ocupado, la comprobación de que ningún asset referencia un origen externo, que una opción abreviada como `--host` no se acepte como `--hosts`, y los límites de los argumentos (`--interval` positivo y finito, `--port` dentro de rango).

Para verificar a mano:

```bash
python3 web.py --demo --port 0     # imprime la URL y el estado de la página
python3 -c "import web"            # confirma que importa sin el .venv
```
