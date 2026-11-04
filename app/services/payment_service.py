import time
import json
import requests
from config import Config
from app.database import crear_registro_pago, actualizar_pago_aprobado, obtener_usuario_por_id


class PaymentService:
    def __init__(self):
        self.mp_api_url = "https://api.mercadopago.com"

    def crear_preferencia_mp(self, user_id, email=None, username=None):
        """
        Genera una preferencia de pago en Mercado Pago Checkout Pro para 30 días de Plan PRO.
        Retorna (ok: bool, resultado: dict|str).
        """
        token = getattr(Config, "MP_ACCESS_TOKEN", "").strip()
        precio = float(getattr(Config, "PRECIO_PLAN_PRO_ARS", 10000))
        public_url = getattr(Config, "PUBLIC_URL", "http://localhost:5000").rstrip("/")

        ext_ref = f"arb_user_{user_id}_{int(time.time())}"
        payer_email = (email or f"{username or 'user'}@arbitraje.com").strip()

        # Si no hay token de Mercado Pago configurado, generamos una respuesta de modo DEMO
        if not token or token.startswith("APP_USR-tu_"):
            print("⚠️ [Mercado Pago] No hay MP_ACCESS_TOKEN válido configurado. Generando link DEMO de prueba.")
            pref_id_mock = f"DEMO_PREF_{int(time.time())}"
            crear_registro_pago(
                user_id=user_id,
                external_reference=ext_ref,
                mp_preference_id=pref_id_mock,
                amount=precio,
                plan_code="PRO"
            )
            # Retorna URL de simulación de retorno directo
            return True, {
                "preference_id": pref_id_mock,
                "init_point": f"{public_url}/pagos/retorno?status=demo&ref={ext_ref}",
                "is_demo": True
            }

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        payload = {
            "items": [
                {
                    "id": "PLAN_PRO_30D",
                    "title": "Suscripción PRO Arbitraje BYMA (30 Días)",
                    "description": "Acceso ilimitado a arbitrajes en tiempo real, precios ejecutables y Telegram",
                    "quantity": 1,
                    "currency_id": "ARS",
                    "unit_price": precio
                }
            ],
            "payer": {
                "email": payer_email
            },
            "external_reference": ext_ref,
            "back_urls": {
                "success": f"{public_url}/pagos/retorno?status=success",
                "pending": f"{public_url}/pagos/retorno?status=pending",
                "failure": f"{public_url}/pagos/retorno?status=failure"
            },
            "auto_return": "approved",
            "notification_url": f"{public_url}/api/webhooks/mercadopago",
            "statement_descriptor": "ARBITRAJE BYMA"
        }

        try:
            r = requests.post(f"{self.mp_api_url}/checkout/preferences", json=payload, headers=headers, timeout=12)
            if r.status_code not in (200, 201):
                err_msg = r.text
                print(f"⛔ [Mercado Pago Error] {r.status_code}: {err_msg}")
                return False, f"Error Mercado Pago ({r.status_code})"

            data = r.json()
            pref_id = data.get("id")
            init_point = data.get("init_point")

            crear_registro_pago(
                user_id=user_id,
                external_reference=ext_ref,
                mp_preference_id=pref_id,
                amount=precio,
                plan_code="PRO"
            )

            return True, {
                "preference_id": pref_id,
                "init_point": init_point,
                "is_demo": False
            }
        except Exception as e:
            print(f"⛔ [Mercado Pago Exception]: {e}")
            return False, f"Error conectando con pasarela de pago: {e}"

    def procesar_notificacion(self, body, query_params):
        """
        Procesa el Webhook / IPN enviado por Mercado Pago.
        Consulta el estado real del pago en la API de MP y activa el Plan PRO al usuario.
        """
        token = getattr(Config, "MP_ACCESS_TOKEN", "").strip()
        payment_id = None

        # 1. Extraer payment_id de query params o body
        if query_params.get("topic") == "payment" or query_params.get("type") == "payment":
            payment_id = query_params.get("id") or query_params.get("data.id")
        elif isinstance(body, dict):
            if body.get("type") == "payment" or body.get("topic") == "payment":
                payment_id = (body.get("data") or {}).get("id") or body.get("id")
            elif "id" in body and body.get("action") == "payment.created":
                payment_id = (body.get("data") or {}).get("id") or body.get("id")

        if not payment_id:
            return False, "No se identificó payment_id en la notificación"

        # 2. Consultar el pago en la API de Mercado Pago
        if not token or token.startswith("APP_USR-tu_"):
            return False, "MP_ACCESS_TOKEN no configurado"

        headers = {"Authorization": f"Bearer {token}"}
        try:
            r = requests.get(f"{self.mp_api_url}/v1/payments/{payment_id}", headers=headers, timeout=10)
            if r.status_code != 200:
                return False, f"Error consultando pago {payment_id} en MP ({r.status_code})"

            pago_data = r.json()
            status = pago_data.get("status")
            ext_ref = pago_data.get("external_reference")
            payer_email = (pago_data.get("payer") or {}).get("email")

            print(f"💳 [Mercado Pago Webhook] Pago {payment_id} | Status: {status} | Ref: {ext_ref}")

            if status == "approved":
                ok, user_id = actualizar_pago_aprobado(
                    mp_payment_id=payment_id,
                    external_reference=ext_ref,
                    payer_email=payer_email,
                    raw_payload=json.dumps(pago_data)
                )
                if ok and user_id:
                    print(f"🎉 [Plan PRO Activado] Usuario ID {user_id} actualizado con éxito por 30 días.")
                    return True, {"user_id": user_id, "status": "approved"}

            return True, {"status": status}
        except Exception as e:
            print(f"⛔ [Error Webhook MP]: {e}")
            return False, str(e)


payment_service = PaymentService()
