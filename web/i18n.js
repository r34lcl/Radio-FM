// Traducción de la interfaz (como en Disk Monitor): el texto en español es la clave; si falta una
// traducción se muestra en español. El idioma lo elige el admin (se guarda en el servidor, que lo
// pone en <html lang>) y vale para toda la web, también la página pública. El reproductor
// insertado (/embed) puede forzar otro con ?lang=es|en.
let LANG = "es", LOC = "es-CL";
(function () {
  let l = null;
  if (location.pathname === "/embed") { try { l = new URLSearchParams(location.search).get("lang"); } catch (e) {} }
  if (l !== "es" && l !== "en") l = document.documentElement.lang === "en" ? "en" : "es";
  LANG = l; LOC = l === "en" ? "en-US" : "es-CL";
  document.documentElement.lang = LANG;
})();

async function setLanguage(l) {
  if (l === LANG) return;
  try {
    const r = await fetch("/api/language", { method: "POST", headers: { "Content-Type": "application/json" },
                                             body: JSON.stringify({ language: l }) });
    if (r.status === 401) { location.href = "/login"; return; }
    if (r.ok) location.reload();
  } catch (e) { /* servidor reiniciando */ }
}

function _t(s, v) {
  let o = (LANG === "en" && I18N_EN[s]) || s;
  if (v) o = o.replace(/\{(\w+)\}/g, (m, k) => (k in v ? v[k] : m));
  return o;
}
const _plural = (n, one, many) => _t(n === 1 ? one : many, { n });

// Mensajes que llegan ya armados del servidor (p.ej. "Probando ganancia 7.7 dB (3 de 29)…"):
// se buscan entre las claves con {marcadores}.
let _patterns = null;
function _tm(msg) {
  if (LANG !== "en" || !msg) return msg;
  if (I18N_EN[msg]) return I18N_EN[msg];
  if (!_patterns) {
    _patterns = Object.keys(I18N_EN).filter(k => k.includes("{")).map(k => {
      const names = [];
      const re = k.replace(/[.*+?^$()|[\]\\]/g, "\\$&").replace(/\\?\{(\w+)\\?\}/g, (m, n) => { names.push(n); return "(.+?)"; });
      return { re: new RegExp("^" + re + "$"), names, key: k };
    });
  }
  for (const p of _patterns) {
    const m = msg.match(p.re);
    if (m) return _t(p.key, Object.fromEntries(p.names.map((n, i) => [n, _tm(m[i + 1])])));
  }
  return msg;
}

// Textos fijos del HTML: data-t (texto), data-th (HTML con <b>…), title, aria-label y placeholder
function tStatic(root) {
  if (LANG === "es") return;
  root = root || document;
  const norm = s => s.replace(/\s+/g, " ").trim();
  root.querySelectorAll("[data-t]").forEach(e => { e.textContent = _t(norm(e.textContent)); });
  root.querySelectorAll("[data-th]").forEach(e => { e.innerHTML = _t(norm(e.innerHTML)); });
  root.querySelectorAll("[title]").forEach(e => { e.title = _t(e.title); });
  root.querySelectorAll("[aria-label]").forEach(e => e.setAttribute("aria-label", _t(e.getAttribute("aria-label"))));
  root.querySelectorAll("[placeholder]").forEach(e => { e.placeholder = _t(e.placeholder); });
  root.querySelectorAll("option").forEach(e => { e.textContent = _t(norm(e.textContent)); });
  if (document.title) document.title = _t(document.title);
}

// Selector ES / EN (mismo diseño que Disk Monitor). Se inserta en los elementos con [data-lang-picker].
function langPicker() {
  return '<div class="lang" role="group" aria-label="Idioma / Language">' +
    '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><circle cx="10" cy="10" r="8"/>' +
    '<path d="M2 10h16M10 2c2.2 2.3 3.2 5 3.2 8s-1 5.7-3.2 8c-2.2-2.3-3.2-5-3.2-8s1-5.7 3.2-8z"/></svg>' +
    ["es", "en"].map(l => `<button type="button" data-lang="${l}" aria-pressed="${l === LANG}" title="${l === "es" ? "Español" : "English"}">${l.toUpperCase()}</button>`).join("") +
    "</div>";
}
document.head.insertAdjacentHTML("beforeend", `<style>
  .lang { display: inline-flex; align-items: center; gap: 2px; padding: 2px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }
  .lang svg { width: 15px; height: 15px; margin: 0 3px 0 5px; color: var(--muted); flex: none; }
  div.lang button { border: 0; background: transparent; color: var(--muted); font: inherit; font-size: 12px; font-weight: 600;
    padding: 3px 8px; border-radius: 6px; cursor: pointer; min-height: 24px; margin: 0; }
  div.lang button[aria-pressed=true] { background: var(--accent); color: #111; }
  div.lang button:not([aria-pressed=true]):hover { background: var(--line); color: var(--text); }
  div.lang button:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
</style>`);
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-lang-picker]").forEach(el => {
    el.insertAdjacentHTML("afterbegin", langPicker());
    el.querySelectorAll(".lang button").forEach(b => b.onclick = e => { e.preventDefault(); setLanguage(b.dataset.lang); });
  });
});

const I18N_EN = {
  // general y navegación
  "Radio FM": "FM Radio", "Radio FM · Admin": "FM Radio · Admin", "Radio FM · Ingresar": "FM Radio · Sign in",
  "Radio FM · Configuración": "FM Radio · Settings", "📻 Radio FM · Admin": "📻 FM Radio · Admin",
  "Página pública": "Public page", "Sintonizador": "Tuner", "Configuración": "Settings", "Salir": "Log out",
  "Login": "Log in", "Usuario": "Username", "Contraseña": "Password", "Ingresar": "Sign in",
  "← Volver a la radio": "← Back to the radio", "Guardar": "Save", "Error": "Error", "Error: {e}": "Error: {e}",
  "No autenticado": "Not signed in", "Se espera JSON": "JSON expected", "Petición inválida": "Invalid request",
  "Demasiados intentos. Espera unos minutos.": "Too many attempts. Wait a few minutes.",
  "No se pudo verificar que no eres un robot. Intenta de nuevo.": "Could not verify that you are not a robot. Try again.",
  "Usuario o contraseña incorrectos.": "Wrong username or password.",
  // reproductor
  "Reproducir": "Play", "Detener": "Stop", "Silenciar": "Mute", "Activar sonido": "Unmute", "Volumen": "Volume",
  "🔊 Usa los botones de volumen de tu dispositivo": "🔊 Use your device's volume buttons",
  "Conectando…": "Connecting…", "En vivo": "Live", "Sin señal": "No signal", "Estéreo": "Stereo", "Mono": "Mono",
  "Cambiando de emisora…": "Changing station…", "En mantención, vuelve en un minuto…": "Under maintenance, back in a minute…",
  "Escaneando la banda…": "Scanning the band…", "Sintonizando…": "Tuning…",
  "{n} oyente": "{n} listener", "{n} oyentes": "{n} listeners", "sin señal": "no signal",
  "Emisoras en vivo": "Live stations", "Ver en Shazam": "View on Shazam", "Radio FM en vivo": "FM Radio live",
  // sintonizador
  "Bajar 0,1 MHz": "Down 0.1 MHz", "Subir 0,1 MHz": "Up 0.1 MHz", "Frecuencia en MHz": "Frequency in MHz",
  "Sintonizar": "Tune", "Nombre de la emisora": "Station name", "Guardar nombre": "Save name",
  "Nombre para {f} MHz": "Name for {f} MHz", "Nombre guardado": "Name saved",
  "La emisora no volvió al aire. Revisa los logs del contenedor tuner.": "The station did not come back on air. Check the tuner container logs.",
  "Frecuencia inválida": "Invalid frequency", "La frecuencia debe estar entre {a} y {b} MHz": "The frequency must be between {a} and {b} MHz",
  "Esa emisora no se está transmitiendo": "That station is not being broadcast", "Sin señal en este momento": "No signal right now",
  "Multi-emisora": "Multi-station",
  "Transmite otras emisoras al mismo tiempo que la principal. Tienen que estar a 2 MHz o menos entre todas, porque el dongle capta un solo bloque del espectro.":
    "Broadcasts other stations at the same time as the main one. They must all fit within 2 MHz, because the dongle captures a single block of spectrum.",
  "No cabe en el bloque junto con las ya elegidas": "Does not fit in the block with the ones already chosen",
  "No hay emisoras conocidas cerca de la principal.": "There are no known stations near the main one.",
  "Centro del bloque: {f} MHz": "Block center: {f} MHz",
  "Mis emisoras": "My stations", "Emisoras detectadas": "Detected stations",
  "Barras = calidad de señal medida en el último escaneo (se repite en Configuración). Con 1 barra conviene usar mono.":
    "Bars = signal quality measured in the last scan (run it again in Settings). With 1 bar, mono works better.",
  "Sin medición de señal": "Signal not measured",
  // registro de oyentes
  "Oyentes": "Listeners", "Solo en línea": "Online only", "Inicio": "Start", "Duración": "Duration", "IP": "IP",
  "Emisora": "Station", "Dispositivo": "Device", "Datos": "Data",
  "Todavía no hay conexiones registradas.": "No connections recorded yet.",
  "<b>{n}</b> en línea · {ips} IP distintas en 24 h": "<b>{n}</b> online · {ips} distinct IPs in 24 h",
  "En línea": "Online", "Desconectado": "Disconnected", "Fin: {d}": "End: {d}", "Hoy {t}": "Today {t}",
  "Ver ubicación aproximada": "View approximate location", "Desconocido": "Unknown",
  "Escuchando desde el reproductor insertado en {s}": "Listening from the player embedded on {s}", "vía {s}": "via {s}",
  // configuración: ganancia
  "Ganancia del sintonizador": "Tuner gain", "Ganancia": "Gain", "Automática": "Automatic", "Auto": "Auto",
  "Más ganancia capta mejor las emisoras débiles, pero si es demasiada el dongle se satura: sube el ruido y el audio se distorsiona. Lo más fácil es usar el escaneo de abajo para elegirla. Al cambiarla, la transmisión se corta unos segundos.":
    "More gain picks up weak stations better, but too much saturates the dongle: noise rises and the audio distorts. The easiest way to choose it is the scan below. Changing it interrupts the broadcast for a few seconds.",
  "Ganancia guardada; la radio se está reiniciando.": "Gain saved; the radio is restarting.",
  "Ganancia no válida para este dongle": "Gain not valid for this dongle",
  // configuración: escaneo
  "Escaneo de la banda": "Band scan",
  "Recorre la banda FM (87,5–108 MHz) probando cada ganancia, recomienda la mejor y detecta las emisoras que se reciben con la antena en su posición actual. Úsalo si mueves la antena o el equipo de lugar. <b>La radio se corta alrededor de 1 minuto</b> mientras dura.":
    "Sweeps the FM band (87.5–108 MHz) trying each gain, recommends the best one and detects the stations received with the antenna in its current position. Use it if you move the antenna or the equipment. <b>The radio goes off air for about 1 minute</b> while it runs.",
  "Escanear ahora": "Scan now", "Iniciando…": "Starting…", "La radio vuelve al aire sola al terminar.": "The radio comes back on air by itself when it finishes.",
  "Deteniendo la radio…": "Stopping the radio…", "Abriendo el dongle…": "Opening the dongle…",
  "Probando ganancia {g} dB ({i} de {n})…": "Trying gain {g} dB ({i} of {n})…",
  "Barrido final con {g} dB…": "Final sweep at {g} dB…",
  "Listo: {n} emisoras, ganancia recomendada {g} dB": "Done: {n} stations, recommended gain {g} dB",
  "No se pudo abrir el dongle RTL-SDR": "Could not open the RTL-SDR dongle", "Error leyendo muestras del dongle": "Error reading samples from the dongle",
  "La radio se va a cortar alrededor de 1 minuto mientras se escanea. ¿Continuar?": "The radio will go off air for about 1 minute during the scan. Continue?",
  "Espera a que termine el escaneo": "Wait for the scan to finish", "No hay un escaneo para aplicar": "There is no scan to apply",
  "Opción de emisoras no válida": "Invalid station option", "Idioma no válido": "Invalid language",
  "Último escaneo: {d}": "Last scan: {d}", "Ganancia recomendada: <b>{g} dB</b>": "Recommended gain: <b>{g} dB</b>",
  " (es la que estás usando).": " (the one you are using).", " (ahora usas {g}).": " (you are using {g}).",
  " Se probaron de menor a mayor hasta que el dongle empezó a recortar o a perder calidad.": " Gains were tried from low to high until the dongle started clipping or losing quality.",
  "Emisoras": "Stations", "Calidad": "Quality", "Recorte": "Clipping",
  "Emisoras detectadas ({n})": "Detected stations ({n})", "Ya no se reciben ({n})": "No longer received ({n})", "nueva": "new",
  "Usar la ganancia recomendada ({g} dB, sin cambios)": "Use the recommended gain ({g} dB, unchanged)",
  "Cambiar la ganancia a la recomendada: <b>{g} dB</b>": "Change the gain to the recommended one: <b>{g} dB</b>",
  "Misma ubicación": "Same location", "Actualiza la señal de cada emisora y conserva los nombres que les pusiste.": "Updates each station's signal and keeps the names you gave them.",
  "Nueva ubicación": "New location", "Borra los nombres y las emisoras extra del modo multi: las frecuencias pueden ser de otras emisoras.":
    "Clears the names and the extra multi-mode stations: the frequencies may belong to other stations now.",
  "No tocar la lista de emisoras": "Leave the station list untouched", "Aplicar resultados": "Apply results",
  "Se borrarán los nombres de todas las emisoras y las emisoras extra. ¿Continuar?": "All station names and extra stations will be cleared. Continue?",
  "Resultados aplicados.": "Results applied.", " La radio se reinicia con la nueva ganancia.": " The radio restarts with the new gain.",
  // configuración: insertar
  "Insertar en otra página": "Embed in another page",
  "Copia este código y pégalo en tu sitio para poner el reproductor público. En <b>WordPress</b>: agrega un bloque <b>HTML personalizado</b> (o el modo «Texto» del editor clásico) y pega el código.":
    "Copy this code and paste it on your site to add the public player. In <b>WordPress</b>: add a <b>Custom HTML</b> block (or the “Text” mode of the classic editor) and paste the code.",
  "Colores": "Colors", "Oscuro": "Dark", "Claro": "Light", "Idioma": "Language",
  "El de la radio": "Same as the radio", "Español": "Spanish", "Inglés": "English",
  "Altura automática": "Automatic height", "Código para insertar": "Embed code", "Copiar código": "Copy code", "Copiado": "Copied",
  "Vista previa": "Preview", "Todas las en vivo (el oyente elige)": "All live stations (the listener chooses)",
  "Con una emisora fija, si esa frecuencia deja de transmitirse el reproductor muestra la principal hasta que vuelva.":
    "With a fixed station, if that frequency stops broadcasting the player shows the main one until it comes back.",
  "El oyente puede elegir entre las emisoras que estén en vivo (si hay más de una).": "The listener can choose among the live stations (if there is more than one).",
};
