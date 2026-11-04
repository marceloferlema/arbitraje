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

function actualizarEstadoVisual(isRunning, provider, errorMsg, activeSpecies, marketStatus, marketOpen, plan, diasRestantes, esPro) {
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

    // Actualización dinámica del plan del usuario
    if (plan) {
        const badgePlan = document.getElementById('badge-plan-container');
        if (badgePlan) {
            if (plan === 'VIP') {
                badgePlan.innerHTML = '<span class="badge bg-warning text-dark"><i class="bi bi-star-fill"></i> VIP</span>';
            } else if (plan === 'PRO') {
                badgePlan.innerHTML = `<span class="badge bg-success" role="button" onclick="abrirModalSuscripcion()"><i class="bi bi-gem"></i> PRO (<span id="txt-dias-plan">${diasRestantes || 0}d</span>)</span>`;
            } else if (plan === 'TRIAL') {
                badgePlan.innerHTML = `<span class="badge bg-info text-dark" role="button" onclick="abrirModalSuscripcion()"><i class="bi bi-clock-history"></i> TRIAL (<span id="txt-dias-plan">${diasRestantes || 0}d</span>) <span class="badge bg-warning text-dark ms-1">💎 $10.000</span></span>`;
            } else {
                badgePlan.innerHTML = '<button class="btn btn-sm btn-warning fw-bold py-0 px-2" onclick="abrirModalSuscripcion()"><i class="bi bi-rocket-takeoff-fill"></i> 💎 Activar PRO ($10.000)</button>';
            }
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
        tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted py-3">Esperando oportunidades ejecutables...</td></tr>';
        document.getElementById('alert-count').innerText = 0;
        return;
    }

    // Lógica de Sonido solo cuando entra una oportunidad genuinamente nueva
    const alertaMasReciente = data[0];
    const idActual = alertaMasReciente ? `${alertaMasReciente.simbolo}_${alertaMasReciente.tipo}_${alertaMasReciente.t0}_${alertaMasReciente.t1}` : null;

    if (ultimaAlertaId !== null && idActual && ultimaAlertaId !== idActual) {
        if (sonidoHabilitado) sonidoNotificacion.play().catch(e => console.error("Error audio:", e));
    }
    ultimaAlertaId = idActual;

    let htmlBuffer = '';

    data.forEach((alerta) => {
        let textoTipo = alerta.tipo.replace('_', ' '); 
        const montoStr = alerta.monto ? '$' + Number(alerta.monto).toLocaleString('es-AR') : '-';
        const nomStr = alerta.nominales ? ` <small class="text-muted">(${alerta.nominales} nom.)</small>` : '';
        const filaId = `row-alerta-${alerta.simbolo}-${alerta.tipo}`;
        
        // Gap Neto con color distintivo
        let varNetaHtml = '-';
        if (alerta.variacion_neta !== undefined) {
            const colorNeta = alerta.variacion_neta > 0 ? 'text-warning' : 'text-danger';
            varNetaHtml = `<span class="fw-bold ${colorNeta}">${alerta.variacion_neta > 0 ? '+' : ''}${alerta.variacion_neta}%</span>`;
        }

        // Puntas CI y 24h (bloqueadas para usuarios FREE en alertas FUERTES)
        let celdaCI = '';
        let celda24h = '';
        let celdaGanancia = '';

        if (alerta.bloqueada_pro) {
            celdaCI = `<button class="btn btn-sm btn-outline-warning py-0 px-2 fw-semibold" style="font-size: 0.8em;" onclick="abrirModalSuscripcion()" title="Desbloquear en Plan PRO"><i class="bi bi-lock-fill"></i> PRO</button>`;
            celda24h = `<button class="btn btn-sm btn-outline-warning py-0 px-2 fw-semibold" style="font-size: 0.8em;" onclick="abrirModalSuscripcion()" title="Desbloquear en Plan PRO"><i class="bi bi-lock-fill"></i> PRO</button>`;
            celdaGanancia = `<span class="badge bg-secondary bg-opacity-25 text-muted py-1 px-2" role="button" onclick="abrirModalSuscripcion()" title="Desbloquear en Plan PRO"><i class="bi bi-lock-fill"></i> PRO</span>`;
        } else {
            celdaCI = `$${alerta.t0}`;
            celda24h = `$${alerta.t1}`;
            const gan = Number(alerta.ganancia_neta_ars || 0);
            const colorGan = gan > 0 ? 'text-success' : 'text-secondary';
            celdaGanancia = `<span class="fw-bold ${colorGan}">+${'$' + gan.toLocaleString('es-AR')}</span>`;
        }

        const t0_val = alerta.t0_raw !== undefined ? alerta.t0_raw : (typeof alerta.t0 === 'number' ? alerta.t0 : 0);
        const t1_val = alerta.t1_raw !== undefined ? alerta.t1_raw : (typeof alerta.t1 === 'number' ? alerta.t1 : 0);
        const var_neta_val = alerta.variacion_neta !== undefined ? alerta.variacion_neta : 0;
        const monto_val = alerta.monto || 0;

        htmlBuffer += `
            <tr id="${filaId}">
                <td>${alerta.hora}</td>
                <td><strong>${alerta.simbolo}</strong></td>
                <td class="text-center">
                    <span class="lbl-base ${alerta.tipo}">${textoTipo}</span>
                </td>
                <td class="text-end fw-bold text-secondary">${alerta.variacion}%</td>
                <td class="text-end">${varNetaHtml}</td>
                <td class="text-end fw-semibold">${celdaCI}</td>
                <td class="text-end fw-semibold">${celda24h}</td>
                <td class="text-end fw-semibold">${montoStr}${nomStr}</td>
                <td class="text-end">${celdaGanancia}</td>
                <td class="text-center">
                    <button class="btn btn-sm btn-outline-primary py-0 px-2" title="Calcular ganancia neta para ${alerta.simbolo}" onclick="abrirCalculadora('${alerta.simbolo}', '${alerta.tipo}', ${t0_val}, ${t1_val}, ${alerta.variacion}, ${var_neta_val}, ${monto_val})">
                        <i class="bi bi-calculator"></i>
                    </button>
                </td>
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
    .then(data => actualizarEstadoVisual(data.running, data.provider, data.error, data.active_species, data.market_status, data.market_open, data.plan, data.dias_restantes, data.es_pro));

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
        .then(data => actualizarEstadoVisual(data.running, data.provider, data.error, data.active_species, data.market_status, data.market_open, data.plan, data.dias_restantes, data.es_pro))
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
                const elCom = document.getElementById('cfg-comision-broker');
                if (elCom) elCom.value = c.comision_broker ?? 0.5;
                const elCap = document.getElementById('cfg-capital-operacion');
                if (elCap) elCap.value = c.capital_operacion ?? 500000;
                document.getElementById('cfg-telegram-chat').value = c.telegram_chat_id || '';
                document.getElementById('cfg-telegram-on').checked = Boolean(c.telegram_on);
                const darkCheck = document.getElementById('cfg-dark-mode');
                if (darkCheck) darkCheck.checked = Boolean(c.dark_mode);
                aplicarTemaVisual(Boolean(c.dark_mode));
                document.getElementById('cfg-nueva-password').value = '';
            }
            modalConfigUsuarioObj.show();
        })
        .catch(err => {
            console.error('Error cargando configuración de usuario:', err);
            modalConfigUsuarioObj.show();
        });
}

// === GESTIÓN DE TEMA OSCURO / CLARO ===
function toggleTheme() {
    const htmlEl = document.documentElement;
    const esOscuro = htmlEl.getAttribute('data-bs-theme') === 'dark';
    const nuevoEstado = !esOscuro;
    
    aplicarTemaVisual(nuevoEstado);

    // Persistimos en la base de datos para el usuario logueado
    fetch('/api/theme', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dark_mode: nuevoEstado })
    }).catch(err => console.error('Error guardando preferencia de tema:', err));
}

function aplicarTemaVisual(esOscuro) {
    const htmlEl = document.documentElement;
    const icon = document.getElementById('icon-theme');
    const chkConfig = document.getElementById('cfg-dark-mode');

    if (esOscuro) {
        htmlEl.setAttribute('data-bs-theme', 'dark');
        if (icon) {
            icon.className = 'bi bi-sun-fill text-warning';
        }
        if (chkConfig) chkConfig.checked = true;
    } else {
        htmlEl.setAttribute('data-bs-theme', 'light');
        if (icon) {
            icon.className = 'bi bi-moon-stars-fill text-dark';
        }
        if (chkConfig) chkConfig.checked = false;
    }
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
        comision_broker: parseFloat(document.getElementById('cfg-comision-broker')?.value || 0.5),
        capital_operacion: parseFloat(document.getElementById('cfg-capital-operacion')?.value || 500000),
        telegram_chat_id: document.getElementById('cfg-telegram-chat').value.trim(),
        telegram_on: document.getElementById('cfg-telegram-on').checked,
        dark_mode: document.getElementById('cfg-dark-mode') ? document.getElementById('cfg-dark-mode').checked : false,
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


// =============================================================================
// 8. MODAL DE SUSCRIPCIÓN PLAN PRO Y MERCADO PAGO
// =============================================================================
let modalSuscripcionObj = null;

function abrirModalSuscripcion() {
    const el = document.getElementById('modalSuscripcionPro');
    if (!el) return;
    if (!modalSuscripcionObj) {
        modalSuscripcionObj = new bootstrap.Modal(el);
    }
    
    const txtPlan = document.getElementById('txt-estado-actual-plan');
    if (txtPlan) txtPlan.innerText = 'Cargando estado de tu plan...';

    fetch('/api/status')
        .then(res => res.json())
        .then(d => {
            if (txtPlan) {
                if (d.plan === 'VIP') {
                    txtPlan.innerText = '🌟 Tu cuenta es Administrador VIP permanente.';
                } else if (d.plan === 'PRO') {
                    txtPlan.innerText = `💎 Tenés Plan PRO Activo (${d.dias_restantes} días restantes). Podés renovar cuando quieras.`;
                } else if (d.plan === 'TRIAL') {
                    txtPlan.innerText = `⏱️ Estás en período de prueba TRIAL (${d.dias_restantes} días restantes). ¡Aprovechá la promo a $10.000 ARS!`;
                } else {
                    txtPlan.innerText = '🔒 Tu plan actual es FREE (limitado a 10 especies). Pasate a PRO para acceso total.';
                }
            }
        })
        .catch(() => {
            if (txtPlan) txtPlan.innerText = 'Plan PRO por $10.000 ARS / mes.';
        });

    modalSuscripcionObj.show();
}

function iniciarPagoMercadoPago() {
    const btn = document.getElementById('btn-iniciar-pago-mp');
    const originalText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Generando checkout seguro...';

    fetch('/api/pagos/crear-preferencia', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'ok' && data.preference && data.preference.init_point) {
                // Redirigir a Mercado Pago Checkout Pro
                window.location.href = data.preference.init_point;
            } else {
                alert('No se pudo generar el pago: ' + (data.message || 'Error desconocido'));
                btn.disabled = false;
                btn.innerHTML = originalText;
            }
        })
        .catch(err => {
            console.error('Error al iniciar pago MP:', err);
            alert('Error de conexión al procesar el pago.');
            btn.disabled = false;
            btn.innerHTML = originalText;
        });
}

// =============================================================================
// 9. CALCULADORA NETA DE ARBITRAJE INTERACTIVA
// =============================================================================
let modalCalculadoraObj = null;
let calcEstado = {
    simbolo: '',
    tipo: '',
    p_in: 0,
    p_out: 0,
    var_bruta: 0
};

function abrirCalculadora(simbolo, tipo, p_in, p_out, var_bruta, var_neta, monto) {
    const el = document.getElementById('modalCalculadora');
    if (!el) return;
    if (!modalCalculadoraObj) {
        modalCalculadoraObj = new bootstrap.Modal(el);
    }

    calcEstado.simbolo = simbolo;
    calcEstado.tipo = tipo;
    calcEstado.p_in = parseFloat(p_in) || 0;
    calcEstado.p_out = parseFloat(p_out) || 0;
    calcEstado.var_bruta = parseFloat(var_bruta) || 0;

    const elSimbolo = document.getElementById('calc-simbolo');
    if (elSimbolo) elSimbolo.innerText = simbolo;

    const badgeTipo = document.getElementById('calc-tipo');
    if (badgeTipo) {
        badgeTipo.innerText = tipo.replace('_', ' ');
        badgeTipo.className = `badge lbl-base ${tipo} ms-2`;
    }

    const elGapBruto = document.getElementById('calc-gap-bruto');
    if (elGapBruto) {
        elGapBruto.innerText = (calcEstado.var_bruta >= 0 ? '+' : '') + calcEstado.var_bruta.toFixed(2) + '%';
    }

    const elPin = document.getElementById('calc-p-in');
    if (elPin) elPin.value = calcEstado.p_in > 0 ? '$' + calcEstado.p_in.toFixed(2) : '-';

    const elPout = document.getElementById('calc-p-out');
    if (elPout) elPout.value = calcEstado.p_out > 0 ? '$' + calcEstado.p_out.toFixed(2) : '-';

    const cfgComision = parseFloat(document.getElementById('cfg-comision-broker')?.value || 0.5);
    const cfgCapital = parseFloat(document.getElementById('cfg-capital-operacion')?.value || 500000);

    const inputComision = document.getElementById('calc-comision');
    if (inputComision) inputComision.value = cfgComision;

    const inputCapital = document.getElementById('calc-capital');
    if (inputCapital) inputCapital.value = cfgCapital;

    recalcularModalCalculadora();
    modalCalculadoraObj.show();
}

function recalcularModalCalculadora() {
    const capital = parseFloat(document.getElementById('calc-capital')?.value || 0);
    const comisionTramo = parseFloat(document.getElementById('calc-comision')?.value || 0);

    const comisionTotalPct = comisionTramo * 2.0; // Entrada CI + Salida 24h
    const gapNetoPct = calcEstado.var_bruta - comisionTotalPct;

    const costoComisionesPesos = capital * (comisionTotalPct / 100.0);
    const gananciaNetaPesos = capital * (gapNetoPct / 100.0);

    const elCosto = document.getElementById('calc-costo-comisiones');
    if (elCosto) elCosto.innerText = `-$${Math.round(costoComisionesPesos).toLocaleString('es-AR')} (${comisionTotalPct.toFixed(2)}%)`;

    const elGapNeto = document.getElementById('calc-gap-neto');
    if (elGapNeto) {
        elGapNeto.innerText = `${gapNetoPct >= 0 ? '+' : ''}${gapNetoPct.toFixed(2)}%`;
        elGapNeto.className = gapNetoPct > 0 ? 'fw-bold text-success' : 'fw-bold text-danger';
    }

    const elGanancia = document.getElementById('calc-ganancia-neta');
    if (elGanancia) {
        if (gananciaNetaPesos > 0) {
            elGanancia.innerText = `+$${Math.round(gananciaNetaPesos).toLocaleString('es-AR')} ARS`;
            elGanancia.className = 'fs-4 fw-bold text-success';
        } else {
            elGanancia.innerText = `-$${Math.abs(Math.round(gananciaNetaPesos)).toLocaleString('es-AR')} ARS`;
            elGanancia.className = 'fs-4 fw-bold text-danger';
        }
    }
}

function guardarCapitalCalculadora() {
    const cap = parseFloat(document.getElementById('calc-capital')?.value || 500000);
    const com = parseFloat(document.getElementById('calc-comision')?.value || 0.5);

    const cfgCap = document.getElementById('cfg-capital-operacion');
    const cfgCom = document.getElementById('cfg-comision-broker');
    if (cfgCap) cfgCap.value = cap;
    if (cfgCom) cfgCom.value = com;

    guardarConfigUsuario();
    alert(`Se guardó tu capital de $${cap.toLocaleString('es-AR')} y comisión de ${com}% como tus valores predeterminados.`);
}

// =============================================================================
// 10. PANEL DE ADMINISTRACIÓN (USUARIOS Y AUDITORÍA DE PAGOS)
// =============================================================================
let modalAdminObj = null;

function abrirModalAdmin() {
    const el = document.getElementById('modalAdmin');
    if (!el) return;
    if (!modalAdminObj) {
        modalAdminObj = new bootstrap.Modal(el);
    }
    modalAdminObj.show();
    cargarDatosAdmin();
}

function cargarDatosAdmin() {
    const tbodyUsers = document.getElementById('tbody-admin-usuarios');
    const tbodyPagos = document.getElementById('tbody-admin-pagos');
    if (tbodyUsers) tbodyUsers.innerHTML = '<tr><td colspan="8" class="text-center py-3 text-muted"><div class="spinner-border spinner-border-sm me-2"></div>Cargando usuarios...</td></tr>';
    if (tbodyPagos) tbodyPagos.innerHTML = '<tr><td colspan="6" class="text-center py-3 text-muted"><div class="spinner-border spinner-border-sm me-2"></div>Cargando pagos...</td></tr>';

    fetch('/api/admin/datos')
        .then(res => res.json())
        .then(data => {
            if (data.status !== 'ok') {
                if (tbodyUsers) tbodyUsers.innerHTML = `<tr><td colspan="8" class="text-center text-danger py-3">${data.message || 'Error'}</td></tr>`;
                return;
            }

            const totalU = document.getElementById('admin-total-usuarios');
            if (totalU) totalU.innerText = data.total_usuarios || (data.usuarios ? data.usuarios.length : 0);

            const totalP = document.getElementById('admin-total-pagos');
            if (totalP) totalP.innerText = data.total_pagos || (data.pagos ? data.pagos.length : 0);

            // Render usuarios
            if (tbodyUsers) {
                if (!data.usuarios || data.usuarios.length === 0) {
                    tbodyUsers.innerHTML = '<tr><td colspan="8" class="text-center text-muted py-3">No hay usuarios registrados.</td></tr>';
                } else {
                    let htmlU = '';
                    data.usuarios.forEach(u => {
                        let badgePlan = 'bg-secondary';
                        if (u.plan === 'VIP') badgePlan = 'bg-warning text-dark';
                        else if (u.plan === 'PRO') badgePlan = 'bg-success';
                        else if (u.plan === 'TRIAL') badgePlan = 'bg-info text-dark';

                        htmlU += `
                            <tr>
                                <td>${u.id}</td>
                                <td class="fw-bold">${u.username}</td>
                                <td>${u.email || '-'}</td>
                                <td><span class="badge ${badgePlan}">${u.plan}</span></td>
                                <td>${u.dias_restantes !== undefined ? u.dias_restantes + 'd' : '-'}</td>
                                <td>${u.telegram_on ? '🟢 Activo' : '⚪ Inactivo'}</td>
                                <td>${u.is_admin ? '<span class="badge bg-dark">Admin</span>' : '<span class="text-muted">Trader</span>'}</td>
                                <td class="text-center">
                                    <div class="btn-group btn-group-sm">
                                        <button class="btn btn-outline-success py-0" onclick="cambiarPlanUsuarioAdmin(${u.id}, 'PRO', 30)" title="Dar 30 días PRO">+30d PRO</button>
                                        <button class="btn btn-outline-warning py-0 text-dark" onclick="cambiarPlanUsuarioAdmin(${u.id}, 'VIP', 365)" title="Dar VIP permanente">VIP</button>
                                        <button class="btn btn-outline-secondary py-0" onclick="cambiarPlanUsuarioAdmin(${u.id}, 'FREE', 0)" title="Bajar a FREE">FREE</button>
                                    </div>
                                </td>
                            </tr>
                        `;
                    });
                    tbodyUsers.innerHTML = htmlU;
                }
            }

            // Render pagos
            if (tbodyPagos) {
                if (!data.pagos || data.pagos.length === 0) {
                    tbodyPagos.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-3">No hay registros de pagos aún.</td></tr>';
                } else {
                    let htmlP = '';
                    data.pagos.forEach(p => {
                        let badgeSt = p.status === 'approved' ? 'bg-success' : (p.status === 'pending' ? 'bg-warning text-dark' : 'bg-danger');
                        htmlP += `
                            <tr>
                                <td>${p.id}</td>
                                <td><strong>${p.username || 'User #' + p.user_id}</strong> <small class="text-muted">(${p.email || ''})</small></td>
                                <td class="fw-bold text-success">$${Number(p.amount || 0).toLocaleString('es-AR')}</td>
                                <td><span class="badge ${badgeSt}">${p.status}</span></td>
                                <td>${p.created_at || '-'}</td>
                                <td><small class="text-muted">${p.mp_payment_id || p.external_reference || '-'}</small></td>
                            </tr>
                        `;
                    });
                    tbodyPagos.innerHTML = htmlP;
                }
            }
        })
        .catch(err => {
            console.error('Error cargando datos admin:', err);
            if (tbodyUsers) tbodyUsers.innerHTML = '<tr><td colspan="8" class="text-center text-danger py-3">Error de red cargando datos de administración.</td></tr>';
        });
}

function cambiarPlanUsuarioAdmin(userId, plan, dias) {
    if (!confirm(`¿Confirmás cambiar al usuario #${userId} al plan ${plan} por ${dias} días?`)) return;

    fetch('/api/admin/usuario/cambiar-plan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, plan: plan, dias: dias })
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'ok') {
                cargarDatosAdmin();
                // Si el usuario modificado es el actual, refrescamos status
                fetch('/api/status')
                    .then(res => res.json())
                    .then(d => actualizarEstadoVisual(d.running, d.provider, d.error, d.active_species, d.market_status, d.market_open, d.plan, d.dias_restantes, d.es_pro));
            } else {
                alert(data.message || 'Error al cambiar plan');
            }
        })
        .catch(err => {
            console.error('Error cambiando plan:', err);
            alert('Error de conexión al cambiar plan.');
        });
}
