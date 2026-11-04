// 1 Conectamos con el WebSocket
const socket = io();

// Variables de audio
let sonidoHabilitado = false;
let ultimaAlertaId = null; 
const sonidoNotificacion = new Audio("https://actions.google.com/sounds/v1/alarms/beep_short.ogg");

// === 2. FUNCIONES DE INTERFAZ (SONIDO, PROVEEDOR Y BOTONES) ===
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
        .then(data => actualizarEstadoVisual(data.running, data.provider, data.error, data.active_species))
        .catch(error => console.error('Error:', error))
        .finally(() => toggleButtons(false));
}

function actualizarEstadoVisual(isRunning, provider, errorMsg, activeSpecies, marketStatus, marketOpen) {
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

    if (activeSpecies !== undefined && activeSpecies !== null) {
        const countBadge = document.getElementById('count-especies-activas');
        if (countBadge) countBadge.innerText = activeSpecies;
    }

    if (marketStatus) {
        const mktEl = document.getElementById('market-status-text');
        if (mktEl) {
            mktEl.innerText = marketStatus;
            mktEl.className = marketOpen ? "text-success fw-semibold" : "text-secondary";
        }
    }

    const errorBanner = document.getElementById('error-banner');
    const errorText = document.getElementById('error-text');
    if (errorBanner && errorText) {
        if (errorMsg) {
            errorText.innerText = errorMsg;
            errorBanner.classList.remove('d-none');
        } else {
            errorBanner.classList.add('d-none');
        }
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

// === 4. LÓGICA DE RENDERIZADO (WEB SOCKETS Y GRILLA ESTABLE) ===
let timeoutRender = null;
let bufferAlertas = null;

function renderizarTabla(data) {
    bufferAlertas = data;
    // Si ya hay un render programado para este segundo, no volvemos a saturar el DOM
    if (!timeoutRender) {
        timeoutRender = setTimeout(() => {
            ejecutarRenderTabla(bufferAlertas);
            timeoutRender = null;
        }, 300); // 300ms de amortiguación para lectura fluida
    }
}

function ejecutarRenderTabla(data) {
    const tbody = document.getElementById('tabla-cuerpo');
    if (!tbody) return;
    
    if (!data || data.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-3">Esperando oportunidades ejecutables...</td></tr>';
        document.getElementById('alert-count').innerText = 0;
        return;
    }

    // Lógica de Sonido solo cuando entra una oportunidad genuinamente nueva
    const alertaMasReciente = data[0];
    const idActual = `${alertaMasReciente.simbolo}_${alertaMasReciente.tipo}_${alertaMasReciente.t0}_${alertaMasReciente.t1}`;

    if (ultimaAlertaId !== null && ultimaAlertaId !== idActual) {
        if (sonidoHabilitado) sonidoNotificacion.play().catch(e => console.error("Error audio:", e));
    }
    ultimaAlertaId = idActual;

    let htmlBuffer = '';

    data.forEach((alerta) => {
        let textoTipo = alerta.tipo.replace('_', ' '); 
        const montoStr = alerta.monto ? '$' + Number(alerta.monto).toLocaleString('es-AR') : '-';
        const nomStr = alerta.nominales ? ` <small class="text-muted">(${alerta.nominales} nom.)</small>` : '';
        const filaId = `row-alerta-${alerta.simbolo}-${alerta.tipo}`;
        
        htmlBuffer += `
            <tr id="${filaId}">
                <td>${alerta.hora}</td>
                <td><strong>${alerta.simbolo}</strong></td>
                <td class="text-center">
                    <span class="lbl-base ${alerta.tipo}">${textoTipo}</span>
                </td>
                <td class="text-end fw-bold text-success">${alerta.variacion}%</td>
                <td class="text-end fw-semibold">$${alerta.t0}</td>
                <td class="text-end fw-semibold">$${alerta.t1}</td>
                <td class="text-end fw-semibold">${montoStr}${nomStr}</td>
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

// B. Carga inicial (pide datos y estado una vez al entrar)
fetch('/api/alertas')
    .then(res => res.json())
    .then(data => renderizarTabla(data));

fetch('/api/status')
    .then(res => res.json())
    .then(data => actualizarEstadoVisual(data.running, data.provider, data.error, data.active_species, data.market_status, data.market_open));

// C. Status Check (Verifica si el bot está corriendo cada 10s)
setInterval(() => {
    fetch('/api/status')
        .then(res => {
            if (res.status === 401) {
                window.location.href = '/login';
                throw new Error('Unauthorized');
            }
            return res.json();
        })
        .then(data => actualizarEstadoVisual(data.running, data.provider, data.error, data.active_species, data.market_status, data.market_open))
        .catch(() => {});
}, 10000);

// === 7. CONFIGURACIÓN PERSONAL DEL USUARIO (UMBRALES, VOLUMEN Y SEGURIDAD) ===
let modalConfigUsuarioObj = null;

function abrirModalConfigUsuario() {
    const modalEl = document.getElementById('modalConfigUsuario');
    if (!modalConfigUsuarioObj) {
        modalConfigUsuarioObj = new bootstrap.Modal(modalEl);
    }

    fetch('/api/config')
        .then(res => res.json())
        .then(data => {
            if (data.status === 'ok' && data.config) {
                const c = data.config;
                document.getElementById('cfg-umbral-fuerte').value = c.umbral_fuerte ?? 1.5;
                document.getElementById('cfg-umbral-normal').value = c.umbral_normal ?? 2.0;
                document.getElementById('cfg-monto-minimo').value = c.monto_minimo ?? 0;
                document.getElementById('cfg-telegram-chat').value = c.telegram_chat_id || '';
                document.getElementById('cfg-telegram-on').checked = Boolean(c.telegram_on);
                document.getElementById('cfg-nueva-password').value = '';
            }
            modalConfigUsuarioObj.show();
        })
        .catch(err => {
            console.error('Error cargando configuración de usuario:', err);
            modalConfigUsuarioObj.show();
        });
}

function guardarConfigUsuario() {
    const btn = document.getElementById('btn-guardar-config-user');
    const textoOriginal = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Guardando...';

    const payload = {
        umbral_fuerte: parseFloat(document.getElementById('cfg-umbral-fuerte').value || 1.5),
        umbral_normal: parseFloat(document.getElementById('cfg-umbral-normal').value || 2.0),
        monto_minimo: parseFloat(document.getElementById('cfg-monto-minimo').value || 0),
        telegram_chat_id: document.getElementById('cfg-telegram-chat').value.trim(),
        telegram_on: document.getElementById('cfg-telegram-on').checked,
        nueva_password: document.getElementById('cfg-nueva-password').value
    };

    fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'ok') {
                if (modalConfigUsuarioObj) modalConfigUsuarioObj.hide();
                fetch('/api/alertas')
                    .then(r => r.json())
                    .then(alertas => renderizarTabla(alertas));
            } else {
                alert(data.message || 'No se pudo guardar la configuración.');
            }
        })
        .catch(err => {
            console.error('Error guardando config usuario:', err);
            alert('Error de red al guardar tu configuración.');
        })
        .finally(() => {
            btn.disabled = false;
            btn.innerHTML = textoOriginal;
        });
}

function limpiarDatos() {
    if (confirm("¿Estás seguro de que quieres borrar todas las alertas de la pantalla y la memoria?")) {
        fetch('/api/limpiar')
            .then(response => response.json())
            .then(data => {
                console.log("Datos limpiados");
            })
            .catch(error => console.error('Error:', error));
    }
}

// === 6. CONFIGURADOR DE ESPECIES (BASE DE DATOS MULTIUSUARIO) ===
let catalogoEspecies = [];
let modalEspeciesObj = null;

function abrirModalEspecies() {
    const modalEl = document.getElementById('modalEspecies');
    if (!modalEspeciesObj) {
        modalEspeciesObj = new bootstrap.Modal(modalEl);
    }
    modalEspeciesObj.show();
    cargarCatalogoEspecies();
}

function cargarCatalogoEspecies() {
    const tbody = document.getElementById('tbody-catalogo-especies');
    tbody.innerHTML = '<tr><td colspan="6" class="text-center py-4 text-muted"><div class="spinner-border spinner-border-sm me-2"></div>Cargando catálogo de especies desde la base de datos...</td></tr>';

    fetch('/api/especies')
        .then(res => res.json())
        .then(data => {
            catalogoEspecies = data.especies || [];
            renderizarCatalogoEspecies();
        })
        .catch(err => {
            console.error('Error cargando catálogo:', err);
            tbody.innerHTML = '<tr><td colspan="6" class="text-center py-4 text-danger">Error cargando especies.</td></tr>';
        });
}

function obtenerEspeciesFiltradas() {
    const texto = (document.getElementById('input-buscar-especie')?.value || '').trim().toUpperCase();
    const tipo = document.getElementById('select-tipo-especie')?.value || 'ALL';
    const estado = document.getElementById('select-estado-especie')?.value || 'ALL';

    return catalogoEspecies.filter(item => {
        if (tipo !== 'ALL' && item.instrument_type !== tipo) return false;
        if (estado === 'ACTIVE' && !item.monitorear) return false;
        if (estado === 'BUY_ONLY' && !item.solo_compra) return false;
        if (estado === 'INACTIVE' && item.monitorear) return false;
        if (texto) {
            const matchTicker = (item.ticker || '').toUpperCase().includes(texto);
            const matchDesc = (item.description || '').toUpperCase().includes(texto);
            if (!matchTicker && !matchDesc) return false;
        }
        return true;
    });
}

function actualizarResumenCatalogo() {
    const totalActivas = catalogoEspecies.filter(x => x.monitorear).length;
    const totalSoloCompra = catalogoEspecies.filter(x => x.monitorear && x.solo_compra).length;
    const badge = document.getElementById('resumen-seleccion-modal');
    if (badge) {
        badge.innerText = `${totalActivas} activas (${totalSoloCompra} solo compra) de ${catalogoEspecies.length} disponibles`;
    }
}

function renderizarCatalogoEspecies() {
    const tbody = document.getElementById('tbody-catalogo-especies');
    const notaLimite = document.getElementById('nota-limite-vista');
    if (!tbody) return;

    actualizarResumenCatalogo();
    const filtradas = obtenerEspeciesFiltradas();

    if (filtradas.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center py-4 text-muted">No se encontraron especies con esos filtros. Si faltan tickers del mercado, pulsá <strong>Actualizar Catálogo BYMA</strong>.</td></tr>';
        if (notaLimite) notaLimite.innerText = '';
        return;
    }

    const MAX_FILAS = 300;
    const visibles = filtradas.slice(0, MAX_FILAS);

    let html = '';
    visibles.forEach(item => {
        const checkedMon = item.monitorear ? 'checked' : '';
        const checkedBuy = item.solo_compra ? 'checked' : '';
        const rowClass = item.monitorear ? 'table-success bg-opacity-10' : '';

        let badgeColor = 'bg-secondary';
        if (item.instrument_type === 'ACCIONES') badgeColor = 'bg-primary';
        else if (item.instrument_type === 'CEDEARS') badgeColor = 'bg-info text-dark';
        else if (item.instrument_type === 'BONOS') badgeColor = 'bg-warning text-dark';
        else if (item.instrument_type === 'ON') badgeColor = 'bg-dark';
        else if (item.instrument_type === 'LETRAS') badgeColor = 'bg-success';

        html += `
            <tr class="${rowClass}" id="row-esp-${item.ticker}">
                <td class="fw-bold">${item.ticker}</td>
                <td class="text-truncate" style="max-width: 280px;" title="${item.description || ''}">${item.description || '-'}</td>
                <td class="text-center"><span class="badge ${badgeColor}">${item.instrument_type}</span></td>
                <td class="text-center"><small class="text-muted">${item.currency || 'ARS'}</small></td>
                <td class="text-center">
                    <div class="form-check form-switch d-inline-block mb-0">
                        <input class="form-check-input" type="checkbox" role="switch"
                            id="chk-mon-${item.ticker}" ${checkedMon}
                            onchange="toggleEspecie('${item.ticker}', 'monitorear', this.checked)">
                    </div>
                </td>
                <td class="text-center">
                    <div class="form-check form-switch d-inline-block mb-0">
                        <input class="form-check-input" type="checkbox" role="switch"
                            id="chk-buy-${item.ticker}" ${checkedBuy}
                            onchange="toggleEspecie('${item.ticker}', 'solo_compra', this.checked)">
                    </div>
                </td>
            </tr>
        `;
    });

    tbody.innerHTML = html;

    if (notaLimite) {
        if (filtradas.length > MAX_FILAS) {
            notaLimite.innerText = `Mostrando las primeras ${MAX_FILAS} de ${filtradas.length} especies filtradas. Usá el buscador para encontrar un ticker específico (los botones masivos afectan a las ${filtradas.length} filtradas).`;
        } else {
            notaLimite.innerText = `Mostrando ${filtradas.length} especies.`;
        }
    }
}

function toggleEspecie(ticker, campo, valor) {
    const item = catalogoEspecies.find(x => x.ticker === ticker);
    if (!item) return;

    if (campo === 'monitorear') {
        item.monitorear = valor ? 1 : 0;
        if (!valor) {
            item.solo_compra = 0;
            const chkBuy = document.getElementById(`chk-buy-${ticker}`);
            if (chkBuy) chkBuy.checked = false;
        }
    } else if (campo === 'solo_compra') {
        item.solo_compra = valor ? 1 : 0;
        if (valor && !item.monitorear) {
            item.monitorear = 1;
            const chkMon = document.getElementById(`chk-mon-${ticker}`);
            if (chkMon) chkMon.checked = true;
        }
    }

    const row = document.getElementById(`row-esp-${ticker}`);
    if (row) {
        row.className = item.monitorear ? 'table-success bg-opacity-10' : '';
    }
    actualizarResumenCatalogo();
}

function marcarVisiblesCatalogo(activar) {
    const filtradas = obtenerEspeciesFiltradas();
    filtradas.forEach(item => {
        item.monitorear = activar ? 1 : 0;
        if (!activar) item.solo_compra = 0;
    });
    renderizarCatalogoEspecies();
}

function guardarConfiguracionEspecies() {
    const btn = document.getElementById('btn-guardar-especies');
    const textoOriginal = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Guardando...';

    const payload = {
        especies: catalogoEspecies.map(x => ({
            ticker: x.ticker,
            monitorear: x.monitorear ? 1 : 0,
            solo_compra: x.solo_compra ? 1 : 0
        }))
    };

    fetch('/api/especies', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'ok') {
                const countBadge = document.getElementById('count-especies-activas');
                if (countBadge) countBadge.innerText = data.activas;
                if (modalEspeciesObj) modalEspeciesObj.hide();
                // Refrescamos alertas por si se desactivó alguna especie
                fetch('/api/alertas')
                    .then(r => r.json())
                    .then(alertas => renderizarTabla(alertas));
            } else {
                alert('Error guardando configuración: ' + (data.message || 'Desconocido'));
            }
        })
        .catch(err => {
            console.error('Error al guardar:', err);
            alert('Error de red al guardar configuración.');
        })
        .finally(() => {
            btn.disabled = false;
            btn.innerHTML = textoOriginal;
        });
}

function sincronizarCatalogoByma() {
    const btn = document.getElementById('btn-sync-byma');
    const textoOriginal = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Sincronizando BYMA...';

    fetch('/api/especies/sync', { method: 'POST' })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'ok') {
                cargarCatalogoEspecies();
            } else {
                alert('No se pudo sincronizar el catálogo: ' + (data.message || ''));
            }
        })
        .catch(err => {
            console.error('Error sincronizando BYMA:', err);
            alert('Error al conectar con el servidor para sincronizar.');
        })
        .finally(() => {
            btn.disabled = false;
            btn.innerHTML = textoOriginal;
        });
}
