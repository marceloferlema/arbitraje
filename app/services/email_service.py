import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from config import Config


class EmailService:
    def _enviar_async(self, destinatario, asunto, cuerpo_html):
        """Ejecuta el envío de correo en un hilo secundario para no demorar la respuesta web."""
        def _job():
            host = getattr(Config, "SMTP_HOST", "").strip()
            port = int(getattr(Config, "SMTP_PORT", 587))
            user = getattr(Config, "SMTP_USER", "").strip()
            password = getattr(Config, "SMTP_PASS", "").strip()
            remitente = getattr(Config, "SMTP_FROM", user or "notificaciones@arbitrajebyma.com")
            use_ssl = bool(getattr(Config, "SMTP_SSL", False))

            if not host or not user or not password or not destinatario:
                return

            try:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = asunto
                msg["From"] = remitente
                msg["To"] = destinatario
                msg.attach(MIMEText(cuerpo_html, "html", "utf-8"))

                if use_ssl or port == 465:
                    server = smtplib.SMTP_SSL(host, port, timeout=12)
                else:
                    server = smtplib.SMTP(host, port, timeout=12)
                    server.starttls()

                server.login(user, password)
                server.sendmail(remitente, [destinatario], msg.as_string())
                server.quit()
                print(f"📧 [Email Enviado] '{asunto}' -> {destinatario}")
            except Exception as e:
                print(f"⚠️ [Error Envío Email] {destinatario} ({asunto}): {e}")

        threading.Thread(target=_job, daemon=True).start()

    def enviar_bienvenida_trial(self, destinatario, username, dias=5):
        """Email de bienvenida al registrarse con activación del Trial PRO."""
        if not destinatario:
            return
        asunto = f"⚡ ¡Bienvenido a Arbitraje BYMA! Activaste tu Trial PRO de {dias} días"
        public_url = getattr(Config, "PUBLIC_URL", "http://localhost:5000")

        html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #212529; border: 1px solid #e9ecef; border-radius: 8px;">
            <div style="background-color: #121417; padding: 16px; border-radius: 6px; text-align: center;">
                <h2 style="color: #ffc107; margin: 0;">⚡ Monitor de Arbitraje BYMA</h2>
                <span style="color: #adb5bd; font-size: 13px;">Acceso en Tiempo Real a Oportunidades CI vs 24hs</span>
            </div>
            
            <div style="padding: 20px 0;">
                <p>Hola <strong>@{username}</strong>,</p>
                <p>Tu cuenta ha sido creada exitosamente. Ya tenés activo tu <strong>Plan TRIAL PRO por {dias} días</strong> con acceso completo e ilimitado:</p>
                
                <ul style="line-height: 1.8;">
                    <li>🚀 <strong>Alertas COMPRA y VENTA FUERTE</strong> con precios ejecutables en tiempo real.</li>
                    <li>📊 Monitoreo de más de <strong>2.050 especies de BYMA</strong> (Acciones, CEDEARs, Bonos, ONs y Letras).</li>
                    <li>📲 <strong>Alertas directas a tu Telegram</strong> personal con Cooldown anti-spam.</li>
                    <li>💰 <strong>Calculadora de Ganancia Neta</strong> configurada con la comisión de tu broker.</li>
                </ul>

                <div style="text-align: center; margin: 30px 0;">
                    <a href="{public_url}" style="background-color: #0d6efd; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
                        Abrir Panel de Arbitrajes
                    </a>
                </div>

                <p style="font-size: 13px; color: #6c757d;">
                    Horario de mercado BYMA: <strong>10:30 a 17:00 hs</strong> para Contado Inmediato (CI) y hasta las 18:00 hs para 24hs.
                </p>
            </div>
            
            <div style="border-top: 1px solid #dee2e6; padding-top: 12px; font-size: 12px; color: #adb5bd; text-align: center;">
                Arbitraje BYMA • Plataforma Profesional de Monitoreo Financiero
            </div>
        </div>
        """
        self._enviar_async(destinatario, asunto, html)

    def enviar_confirmacion_pro(self, destinatario, username, dias=30):
        """Email de confirmación tras pago exitoso de suscripción PRO."""
        if not destinatario:
            return
        asunto = "💎 ¡Plan PRO Activado! Tu suscripción a Arbitraje BYMA está lista"
        public_url = getattr(Config, "PUBLIC_URL", "http://localhost:5000")

        html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #212529; border: 1px solid #e9ecef; border-radius: 8px;">
            <div style="background-color: #198754; padding: 16px; border-radius: 6px; text-align: center;">
                <h2 style="color: white; margin: 0;">💎 ¡Suscripción PRO Activada!</h2>
                <span style="color: #e9ecef; font-size: 13px;">Pago Aprobado por Mercado Pago</span>
            </div>
            
            <div style="padding: 20px 0;">
                <p>Hola <strong>@{username}</strong>,</p>
                <p>Confirmamos la recepción de tu pago. Se han acreditado <strong>{dias} días de Plan PRO</strong> a tu cuenta.</p>
                
                <div style="background-color: #f8f9fa; border-left: 4px solid #198754; padding: 12px 16px; margin: 20px 0;">
                    <p style="margin: 0; font-weight: bold;">Estado: PRO Activo</p>
                    <p style="margin: 4px 0 0 0; font-size: 14px; color: #495057;">Disponés de acceso a todas las oportunidades sin demoras ni restricciones.</p>
                </div>

                <div style="text-align: center; margin: 30px 0;">
                    <a href="{public_url}" style="background-color: #198754; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
                        Ir a Mi Panel PRO
                    </a>
                </div>
            </div>
            
            <div style="border-top: 1px solid #dee2e6; padding-top: 12px; font-size: 12px; color: #adb5bd; text-align: center;">
                Gracias por confiar en Arbitraje BYMA.
            </div>
        </div>
        """
        self._enviar_async(destinatario, asunto, html)

    def enviar_resumen_cierre_mercado(self, destinatario, username, top_oportunidades):
        """Email enviado al cierre de mercado para usuarios FREE con las mejores oportunidades del día (Efecto FOMO)."""
        if not destinatario or not top_oportunidades:
            return
        asunto = "📊 Resumen de Mercado: Las oportunidades de hoy en Arbitraje BYMA"
        public_url = getattr(Config, "PUBLIC_URL", "http://localhost:5000")

        filas_html = ""
        for op in top_oportunidades[:5]:
            filas_html += f"""
            <tr>
                <td style="padding: 8px; border-bottom: 1px solid #dee2e6;"><strong>{op.get('simbolo')}</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #dee2e6;">{op.get('tipo')}</td>
                <td style="padding: 8px; border-bottom: 1px solid #dee2e6; color: #198754; font-weight: bold;">+{op.get('variacion')}%</td>
                <td style="padding: 8px; border-bottom: 1px solid #dee2e6;">${int(op.get('monto', 0)):,}</td>
                <td style="padding: 8px; border-bottom: 1px solid #dee2e6; color: #6c757d;">{op.get('hora')}</td>
            </tr>
            """

        html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #212529; border: 1px solid #e9ecef; border-radius: 8px;">
            <div style="background-color: #121417; padding: 16px; border-radius: 6px; text-align: center;">
                <h2 style="color: #ffc107; margin: 0;">⚡ Rueda del Día Finalizada</h2>
                <span style="color: #adb5bd; font-size: 13px;">Oportunidades destacadas de arbitraje BYMA</span>
            </div>
            
            <div style="padding: 20px 0;">
                <p>Hola <strong>@{username}</strong>,</p>
                <p>Hoy se registraron importantes desarbitrajes en la rueda de Contado Inmediato vs 24hs:</p>

                <table style="width: 100%; border-collapse: collapse; font-size: 14px; margin: 16px 0;">
                    <thead>
                        <tr style="background-color: #f8f9fa; text-align: left;">
                            <th style="padding: 8px; border-bottom: 2px solid #dee2e6;">Ticker</th>
                            <th style="padding: 8px; border-bottom: 2px solid #dee2e6;">Operación</th>
                            <th style="padding: 8px; border-bottom: 2px solid #dee2e6;">Gap</th>
                            <th style="padding: 8px; border-bottom: 2px solid #dee2e6;">Volumen</th>
                            <th style="padding: 8px; border-bottom: 2px solid #dee2e6;">Hora</th>
                        </tr>
                    </thead>
                    <tbody>
                        {filas_html}
                    </tbody>
                </table>

                <p>Activá tu <strong>Plan PRO</strong> para recibir estas alertas en tiempo real directo en tu celular y operar con los precios ejecutables.</p>

                <div style="text-align: center; margin: 30px 0;">
                    <a href="{public_url}" style="background-color: #ffc107; color: #212529; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
                        💎 Activar Plan PRO con Descuento
                    </a>
                </div>
            </div>
            
            <div style="border-top: 1px solid #dee2e6; padding-top: 12px; font-size: 12px; color: #adb5bd; text-align: center;">
                Arbitraje BYMA
            </div>
        </div>
        """
        self._enviar_async(destinatario, asunto, html)


email_service = EmailService()
