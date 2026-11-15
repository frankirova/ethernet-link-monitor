# Dejar el monitor corriendo

Dos procesos distintos, y conviene tenerlos claros:

| Proceso | Qué hace | Si se cae |
| --- | --- | --- |
| `monitor.py --owner` | Escanea por SNMP, manda las alertas y **publica el snapshot** que leen el panel y `--status`. Toma el lock de monitor único. | Deja de monitorear. Nadie te avisa. |
| `web.py` | Sirve el panel; **sólo lee** el snapshot del monitor. | La página dice `SIN SERVIDOR`. El monitoreo sigue. |

Los units de esta carpeta ponen los dos bajo `systemd --user`, que es lo único que garantiza que vuelvan después de un corte o un reinicio.

## `--owner` y `--headless` no son intercambiables

| Modo | Publica snapshot | Toma el lock | Para qué |
| --- | --- | --- | --- |
| `--owner` | Sí | Sí | Bajo un supervisor (estos units). El panel y `--status` lo ven. |
| `--headless` | No | No | Sólo volcar un bloque por escaneo a stdout. Invisible para el panel, y un segundo monitor podría arrancar al lado. |

Si el panel dice `DISCONNECTED` con el monitor supuestamente corriendo, casi seguro el unit está usando `--headless`.

## Instalación

```bash
cd ~/Documentos/work/ethernet-link-monitor
mkdir -p ~/.config/systemd/user
cp deploy/ethernet-monitor.service deploy/ethernet-monitor-web.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ethernet-monitor.service
systemctl --user enable --now ethernet-monitor-web.service
```

Antes de habilitarlos, parar cualquier monitor que hayas arrancado a mano (el unit falla con `monitor already running` y entra en un ciclo de reinicio):

```bash
.venv/bin/python monitor.py --stop --hosts hosts.activos.csv
pkill -f "web.py --port 8765"
```

**Aviso**: reiniciar el monitor pierde la confirmación de dos escaneos y puede reenviar una alerta activa. Si hay un enlace caído ahora, podés recibir el mensaje de nuevo.

## Para que sobreviva al cierre de sesión

```bash
loginctl enable-linger "$USER"
```

Sin `linger`, `/run/user/1000` se borra al cerrar sesión: el monitor sigue escaneando y mandando Telegram, pero **su socket desaparece**, nadie puede hablarle, y un `monitor.py` nuevo arrancaría un segundo monitor (alertas duplicadas). Con `linger` el directorio se recrea al arrancar y sobrevive a los logins.

## Uso diario

```bash
systemctl --user status ethernet-monitor.service ethernet-monitor-web.service
journalctl --user -u ethernet-monitor -f        # alertas y errores, en vivo
journalctl --user -u ethernet-monitor-web -f
systemctl --user restart ethernet-monitor.service
systemctl --user stop ethernet-monitor-web.service ethernet-monitor.service
```

El panel queda en `http://127.0.0.1:8765/`.

## Detalles que importan

- Los units asumen el checkout en `~/Documentos/work/ethernet-link-monitor`. Si movés el repo, editá las rutas (`%h` es tu home).
- Las credenciales siguen saliendo de `.env` al lado de `monitor.py`, porque `WorkingDirectory` es el repo. Ese archivo no está en git.
- `hosts.csv` y `hosts.activos.csv` tampoco están en git; un clon nuevo necesita crearlos a partir de `hosts.example.csv`.
- Si el unit del monitor está reiniciándose justo cuando arranca el del panel, `web.py` intenta levantar un monitor propio: el lock del monitor lo rechaza, el panel avisa por `stderr` y sigue sirviendo. No quedan dos monitores.
