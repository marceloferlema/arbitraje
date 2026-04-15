// 1 Conectamos con el WebSocket
const socket = io();

// Variables de audio
let sonidoHabilitado = false;
let ultimaAlertaId = null; 
const sonidoNotificacion = new Audio("https://actions.google.com/sounds/v1/alarms/beep_short.ogg");

// === 2. FUNCIONES DE INTERFAZ (SONIDO Y BOTONES) ===
function toggleSound() {
    sonidoHabilitado = !sonidoHabilitado;
    const btn = document.getElementById('btn-sound');
    const icon = document.getElementById('icon-sound');
    
    if (sonidoHabilitado) {
        btn.className = "btn btn-primary";
        icon.className = "bi bi-volume-up-fill";
        sonidoNotificacion.play().catch(e => console.log("Audio desbloqueado"));
    } else {
        btn.className = "btn btn-outline-secondary";
        icon.className = "bi bi-volume-mute-fill";
    }
}

function controlBot(endpoint) {
    toggleButtons(true);
    fetch('/' + endpoint)
        .then(response => response.json())
        .then(data => actualizarEstadoVisual(data.running))
        .catch(error => console.error('Error:', error))
        .finally(() => toggleButtons(false));
}

function actualizarEstadoVisual(isRunning) {
    const dot = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    const btnStart = document.getElementById('btn-start');
    const btnStop = document.getElementById('btn-stop');

    if (isRunning) {
        dot.className = 'status-dot status-running';
        text.innerText = "EJECUTANDO";
        text.className = "text-success fw-bold";
        btnStart.classList.add('active');
        btnStop.classList.remove('active');
    } else {
        dot.className = 'status-dot status-stopped';
        text.innerText = "DETENIDO";
        text.className = "text-danger fw-bold";
        btnStart.classList.remove('active');
        btnStop.classList.add('active');
    }
}

function toggleButtons(disabled) {
    document.getElementById('btn-start').disabled = disabled;
    document.getElementById('btn-stop').disabled = disabled;
}

// === 3. LÓGICA DE FILTROS (DROPDOWN) ===
function aplicarFiltros() {
    // Leemos los 4 estados del menú
    const showCompraFuerte = document.getElementById('check-compra-fuerte').checked;
    const showCompra       = document.getElementById('check-compra').checked;
    const showVentaFuerte  = document.getElementById('check-venta-fuerte').checked;
    const showVenta        = document.getElementById('check-venta').checked;
    
    const filas = document.querySelectorAll('#tabla-cuerpo tr');
    let visibles = 0;

    filas.forEach(fila => {
        const span = fila.querySelector('span.lbl-base');
        if (span) {
            const clases = span.className;
            let mostrar = false;

            // Verificación exacta de clases
            if (clases.includes('COMPRA_FUERTE')) {
                if (showCompraFuerte) mostrar = true;
            } else if (clases.includes('COMPRA')) { // Compra Normal
                if (showCompra) mostrar = true;
            } else if (clases.includes('VENTA_FUERTE')) {
                if (showVentaFuerte) mostrar = true;
            } else if (clases.includes('VENTA')) { // Venta Normal
                if (showVenta) mostrar = true;
            }

            if (mostrar) {
                fila.style.display = ''; 
                visibles++;
            } else {
                fila.style.display = 'none'; 
            }
        }
    });

    document.getElementById('alert-count').innerText = visibles;
}

// === 4. LÓGICA DE RENDERIZADO (WEB SOCKETS) ===

// Esta función PINTA la tabla con los datos que recibe (ya sea de la API o del Socket)
function renderizarTabla(data) {
    const tbody = document.getElementById('tabla-cuerpo');
    
    if (!data || data.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-3">Esperando oportunidades...</td></tr>';
        document.getElementById('alert-count').innerText = 0;
        return;
    }

    // Lógica de Sonido
    const alertaMasReciente = data[0];
    const idActual = `${alertaMasReciente.hora}_${alertaMasReciente.simbolo}_${alertaMasReciente.tipo}`;

    if (ultimaAlertaId !== null && ultimaAlertaId !== idActual) {
        if (sonidoHabilitado) sonidoNotificacion.play().catch(e => console.error("Error audio:", e));
    }
    ultimaAlertaId = idActual;

    // Detectar hora del último scan para resaltar
    const horaUltimoScan = data[0].hora;
    let htmlBuffer = '';

    data.forEach((alerta, index) => {
        const claseAnimacion = (index === 0 && ultimaAlertaId === idActual) ? 'nueva-fila' : ''; 
        const claseReciente = (alerta.hora === horaUltimoScan) ? 'latest-scan' : '';
        let textoTipo = alerta.tipo.replace('_', ' '); 
        
        htmlBuffer += `
            <tr class="${claseAnimacion} ${claseReciente}">
                <td>${alerta.hora}</td>
                <td><strong>${alerta.simbolo}</strong></td>
                <td class="text-center">
                    <span class="lbl-base ${alerta.tipo}">${textoTipo}</span>
                </td>
                <td class="text-end fw-bold">${alerta.variacion}%</td>
                <td class="text-end">$${alerta.t0}</td>
                <td class="text-end">$${alerta.t1}</td>
            </tr>
        `;
    });
    
    tbody.innerHTML = htmlBuffer;
    
    const ahora = new Date().toLocaleTimeString();
    document.getElementById('last-update').innerText = 'Última sincro: ' + ahora;
    
    aplicarFiltros(); // Importante: re-aplicar filtros sobre los nuevos datos
}

// === 5. EVENTOS DEL SOCKET Y CARGA INICIAL ===

// A. Escuchar el evento 'actualizacion_alertas' que envía el servidor
socket.on('actualizacion_alertas', function(data) {
    console.log("⚡ Recibida actualización via WebSocket");
    renderizarTabla(data);
});

// B. Carga inicial (pide datos una vez al entrar por si ya había alertas viejas)
fetch('/api/alertas')
    .then(res => res.json())
    .then(data => renderizarTabla(data));

// C. Status Check (Solo verifica si el bot está corriendo cada 10s, no trafica datos)
setInterval(() => {
    fetch('/api/status')
        .then(res => res.json())
        .then(data => actualizarEstadoVisual(data.running));
}, 10000);

function limpiarDatos() {
    if (confirm("¿Estás seguro de que quieres borrar todas las alertas de la pantalla y la memoria?")) {
        fetch('/api/limpiar')
            .then(response => response.json())
            .then(data => {
                console.log("Datos limpiados");
                // No hace falta hacer nada más, el WebSocket 
                // recibirá la lista vacía [] y limpiará la tabla automáticamente.
            })
            .catch(error => console.error('Error:', error));
    }
}