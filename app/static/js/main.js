// === 1. CONFIGURACIÓN DE AUDIO ===
let sonidoHabilitado = false;
let ultimaAlertaId = null;
const sonidoNotificacion = new Audio("https://actions.google.com/sounds/v1/alarms/beep_short.ogg");

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

// === 2. CONTROL DEL BOT ===
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

// === 3. LÓGICA DE FILTROS PARA 4 OPERACIONES ===
function aplicarFiltros() {
    // Leemos los 4 estados
    const showCompraFuerte = document.getElementById('check-compra-fuerte').checked;
    const showCompra       = document.getElementById('check-compra').checked;
    const showVentaFuerte  = document.getElementById('check-venta-fuerte').checked;
    const showVenta        = document.getElementById('check-venta').checked;
    
    const filas = document.querySelectorAll('#tabla-cuerpo tr');
    let visibles = 0;

    filas.forEach(fila => {
        const span = fila.querySelector('span.lbl-base');
        if (span) {
            // Obtenemos la clase exacta (ej: 'lbl-base COMPRA_FUERTE')
            const clases = span.className;
            let mostrar = false;

            // Verificación exacta
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

// === 4. ACTUALIZACIÓN DE DATOS ===
function actualizarTabla() {
    fetch('/api/status')
        .then(res => res.json())
        .then(statusData => actualizarEstadoVisual(statusData.running));

    fetch('/api/alertas')
        .then(response => response.json())
        .then(data => {
            const tbody = document.getElementById('tabla-cuerpo');
            
            if (data.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-3">Sin alertas recientes...</td></tr>';
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

            // === NUEVO: Detectar la hora del último escaneo ===
            // Como la lista viene ordenada por novedad, la primera hora es la última registrada.
            const horaUltimoScan = data[0].hora;

            let htmlBuffer = '';
            data.forEach((alerta, index) => {
                // Animación de entrada (solo la primera vez)
                const claseAnimacion = (index === 0 && ultimaAlertaId === idActual) ? 'nueva-fila' : ''; 
                
                // === NUEVO: Resaltado de lote reciente ===
                // Si la hora de esta alerta coincide con la más nueva, es del último barrido.
                const claseReciente = (alerta.hora === horaUltimoScan) ? 'latest-scan' : '';

                let textoTipo = alerta.tipo.replace('_', ' '); 
                
                // Agregamos ${claseReciente} al TR
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
            
            aplicarFiltros(); 
        })
        .catch(error => console.error('Error fetching alertas:', error));
}

// Iniciar
actualizarTabla();
setInterval(actualizarTabla, 5000);