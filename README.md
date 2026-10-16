# yt-stream

Proxy **RTSP → YouTube Live**: toma el video de una cámara IP sin RTMP (EZVIZ, Hikvision, Dahua, etc.) y lo empuja a YouTube.

- Un solo script en Python, sin dependencias (solo ffmpeg, que se instala solo).
- Linux (systemd) y Windows (tarea programada como SYSTEM).
- Arranca con la computadora y se recupera solo de cortes de luz, de internet o de la cámara.
- Detecta el códec: H.264 se copia sin recodificar; H.265 se pasa a H.264.
- En Linux con GPU Intel recodifica por hardware (VAAPI) a la resolución original, casi sin CPU. Si la GPU falla, sigue con la CPU a 1080p.
- **Panel web con PIN** para manejarlo desde el celular en la red local: iniciar, pausar, reanudar, finalizar y programar horarios.
- Pausa con placa "Transmisión en pausa" (el vivo sigue abierto: mismo video en YouTube).
- Opcional: cada jornada como un vivo separado con título propio (ej. "Torneo Nacional de Bowling - 16/10/2026").
- Evita que la máquina se suspenda mientras transmite.

## Instalación

**Linux**
```bash
git clone https://github.com/Lautaroscu/yt-stream && cd yt-stream
./install.sh
sudo nano /etc/yt-stream.conf
sudo python3 yt_stream.py restart
```

**Windows**
1. Descargar el repo (Code → Download ZIP) y descomprimirlo.
2. Doble click en `install.bat` (pide permisos de administrador; instala Python si falta).
3. `notepad C:\ProgramData\yt-stream\yt-stream.conf`
4. En una consola como administrador: `python yt_stream.py restart`

## Configuración

```ini
CAM_URL="rtsp://admin:ABCDEF@192.168.1.45:554/Streaming/Channels/101"
CAM_MAC="BC:51:FE:12:34:56"   # opcional: si la cámara cambia de IP, la encuentra sola
YT_KEY="abcd-1234-efgh-5678-ijkl"
TITULO="Torneo Nacional de Bowling - {fecha}"   # requiere `auth`
PRIVACIDAD="public"     # public | unlisted | private
HORARIO=""              # vacío = siempre · "manual" = solo a mano · ver abajo
PANEL_PORT="8080"
PANEL_PIN=""            # vacío = install genera uno de 6 dígitos
VIDEO_BITRATE="4000k"   # opcional, solo si recodifica con la CPU
```

- **EZVIZ / Hikvision:** usuario `admin`, contraseña = código de verificación (etiqueta de la cámara). Activar RTSP desde la app. `101` = main stream, `102` = substream.
- **YT_KEY:** YouTube Studio → Emitir en vivo → Transmisión. Usar clave **persistente** y activar **inicio/fin automático**.
- Reservar en el router (DHCP) la IP de la cámara y la de la PC, para que no cambien. Como respaldo, con `CAM_MAC` cargada: si la cámara no responde 3 veces seguidas, barre la red (/24), la busca por MAC y corrige la IP de `CAM_URL` en el `.conf` (como mucho una búsqueda cada 5 min).

Ubicación del `.conf`: `/etc/yt-stream.conf` (Linux) · `C:\ProgramData\yt-stream\yt-stream.conf` (Windows). Reinstalar no lo pisa.

### Panel web

Al instalar se muestra la dirección y el PIN, por ejemplo `http://192.168.1.20:8080   PIN: 417838`. Se abre desde cualquier celular o PC conectado a la misma red.

| Botón | Qué hace |
|---|---|
| **Iniciar transmisión** | Arranca ahora. Si hay horario, dura hasta el fin del horario de ese día; si no, hasta *Finalizar*. |
| **Pausar** | Saca la cámara del aire y muestra una placa "Transmisión en pausa". El vivo sigue abierto (mismo video). |
| **Reanudar** | Vuelve a la cámara. |
| **Finalizar** | Corta y YouTube cierra el vivo. Si había horario, vuelve sola en el próximo. |

**Título del vivo:** se edita desde el panel (`{fecha}` = fecha del día). Con *Cambiar también el vivo que está al aire* se renombra el vivo en curso; si no, aplica desde el próximo. Requiere la cuenta autorizada (`auth`).

**Programación:** *Por horario* (días y horas de inicio/fin), *Siempre* (24 h) o *Solo manual*. Un horario que termina después de medianoche (18:00 a 02:00) pertenece al día en que empieza. También se puede editar `HORARIO` en el `.conf`, sin reiniciar:

```ini
HORARIO="lun-vie 18:00-02:00, sab-dom 14:00-03:00"
HORARIO="manual"
```

El PIN se cambia en `PANEL_PIN` del `.conf` (y `restart`). Después de 5 intentos fallidos el panel se bloquea 60 s para esa IP. El panel también muestra la vista de la cámara y el registro.

### Título por día (API de YouTube)

Opcional. Sin autorizar la cuenta, todos los vivos salen con el título por defecto de YouTube Studio (se renombran desde Studio). Con la cuenta autorizada, cada sesión (inicio por horario o *Iniciar*) crea su propio vivo con `TITULO`, y al cortar lo cierra: cada día queda como un video separado. Un corte corto de internet o de la cámara **no** abre otro vivo, retoma el mismo.

Configuración, una sola vez:

1. [Google Cloud Console](https://console.cloud.google.com/): crear un proyecto y habilitar **YouTube Data API v3**.
2. **Pantalla de consentimiento OAuth:** tipo *Externo*, agregar el email de la cuenta del canal y **publicar la app** ("En producción"). Si queda en *Testing*, el token vence a los 7 días.
3. **Credenciales → Crear ID de cliente OAuth → App de escritorio** y descargar el JSON.
4. En la PC del bowling: `yt_stream.py auth client_secret.json` (con sudo / como administrador). Se abre el navegador, iniciás sesión con la cuenta del canal y aceptás (Google avisa que la app no está verificada: *Configuración avanzada → Ir a…*).
5. `yt_stream.py restart`.

El script crea su propia clave de ingesta en el canal, así que no interfiere con la clave por defecto de Studio. Si la API falla, usa `YT_KEY` como respaldo (con el título por defecto).

## Comandos

Linux con `sudo python3`, Windows con `python` en consola de administrador:

| Comando | |
|---|---|
| `yt_stream.py install` | Instala ffmpeg y el servicio (arranca con la PC) |
| `yt_stream.py auth client_secret.json` | Autoriza la cuenta de YouTube (títulos por día) |
| `yt_stream.py restart` | Reinicia (después de editar el `.conf`) |
| `yt_stream.py live` / `end` | Inicia ahora / finaliza (igual que en el panel) |
| `yt_stream.py pause` / `resume` | Pausa (placa) / reanuda |
| `yt_stream.py stop` / `start` | Detiene / arranca el servicio |
| `yt_stream.py status` | Estado actual + últimas líneas de log |
| `yt_stream.py logs` | Logs en vivo (Ctrl+C para salir) |
| `yt_stream.py uninstall` | Saca el servicio (conserva el `.conf`) |
| `yt_stream.py run` | Corre en primer plano, para probar |

Logs: `/opt/yt-stream/stream.log` · `C:\ProgramData\yt-stream\stream.log`

## Notas

- **Notebooks:** en Windows configurar "Al cerrar la tapa: No hacer nada". En Linux lo cubre `systemd-inhibit`.
- **Red:** la PC tiene que estar en la misma red local que la cámara (no en la red de invitados). Mejor por cable. El panel solo es accesible desde esa red.
- **Subida:** ~4–6 Mbps estables para 1080p.
- Si la cámara no tiene micrófono se envía audio en silencio (YouTube lo necesita).
