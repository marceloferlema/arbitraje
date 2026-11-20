"""
Módulo Radar Previsor: escanea las franjas de Volume Profile, POCs y niveles de liquidez
más cercanos al precio actual para proyectar los techos y pisos operativos de las próximas 4 horas.
"""
from .alerts import _p, fmt_ts


def format_radar_report(runners):
    """
    Construye un reporte unificado para Telegram / consola con los techos, pisos y focos
    operativos de cada par para las próximas 4 horas.
    """
    lines = [
        "🔭 RADAR PREVISOR CRIPTO (Próximas 4 Horas)",
        "Niveles Clave de Volume Profile & Liquidez Institucional:\n",
    ]

    for r in runners:
        if r.ctx is None or r.h1 is None or len(r.h1) == 0:
            continue
        try:
            eng = r.engine()
            t = len(eng.times) - 1
            d = eng.describe(t, r.ctx)
            px = d["price"]
            if r.partial_1h is not None and len(r.partial_1h) > 0:
                px = float(r.partial_1h["close"].iloc[-1])
            else:
                try:
                    px = float(r.ds.ticker_price(r.cfg.symbol))
                except Exception:
                    pass
            sym = r.cfg.symbol
            bias = r.ctx.structure.bias
            trend = r.ctx.structure.trend

            if bias == "DISTRIBUCION":
                foco = "🔴 SHORT (esperar barrido en techos / resistencias)"
            elif bias == "ACUMULACION":
                foco = "🟢 LONG (esperar barrido en pisos / soportes)"
            else:
                foco = "🟡 RANGO (atento a barridos en extremos)"

            # Zonas estrictamente arriba y abajo del precio
            strictly_above = [z for z in r.ctx.zones if z.low > px]
            strictly_below = [z for z in r.ctx.zones if z.high < px]
            inside = [z for z in r.ctx.zones if z.low <= px <= z.high]

            # Techo vendedor más cercano
            techo = "-"
            if strictly_above:
                z = min(strictly_above, key=lambda x: x.low)
                dist = (z.mid / px - 1) * 100
                poc_tag = " · POC" if z.is_poc else ""
                wall_pct = int(round(z.wall_score * 100))
                techo = f"{_p(z.low)} – {_p(z.high)} (+{dist:.1f}% | muralla {wall_pct}%{poc_tag})"
            elif d.get("last_high"):
                lh = d["last_high"][1]
                dist = (lh / px - 1) * 100
                techo = f"{_p(lh)} (+{dist:.1f}% | Swing High liquidez)"

            # Piso comprador más cercano
            piso = "-"
            if strictly_below:
                z = max(strictly_below, key=lambda x: x.high)
                dist = (1 - z.mid / px) * 100
                poc_tag = " · POC" if z.is_poc else ""
                wall_pct = int(round(z.wall_score * 100))
                piso = f"{_p(z.low)} – {_p(z.high)} (-{dist:.1f}% | muralla {wall_pct}%{poc_tag})"
            elif d.get("last_low"):
                ll = d["last_low"][1]
                dist = (1 - ll / px) * 100
                piso = f"{_p(ll)} (-{dist:.1f}% | Swing Low liquidez)"

            lines.append(f"📍 {sym}  |  Precio: {_p(px)} USDT  |  1D: {trend} · {bias}")
            if inside:
                z_in = inside[0]
                lines.append(f"   ⚡ Cotizando en zona: {_p(z_in.low)} – {_p(z_in.high)} (muralla {int(round(z_in.wall_score * 100))}%)")
            lines.append(f"   🔺 Techo Vendedor: {techo}")
            lines.append(f"   🔻 Piso Comprador: {piso}")
            lines.append(f"   🎯 Foco: {foco}\n")
        except Exception as e:
            lines.append(f"📍 {r.cfg.symbol}: error generando radar ({e})\n")

    return "\n".join(lines).strip()
