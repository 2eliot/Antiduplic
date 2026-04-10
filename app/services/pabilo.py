from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests


PABILO_DEFAULT_MOVEMENT_TYPE = "GENERIC"
REFERENCE_ONLY_FIELD_NAMES = {"REFERENCE_NUMBER"}
PABILO_ACCEPTED_STATUSES = {
    "verified",
    "approve",
    "approved",
    "aprobado",
    "success",
    "successful",
    "completed",
    "completada",
    "paid",
    "pagado",
}


def _coerce_decimal_amount(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))

    raw_value = str(value or "").strip()
    if not raw_value:
        return None

    cleaned_value = raw_value.upper()
    for token in ("BSD", "BS.D", "BS", "$"):
        cleaned_value = cleaned_value.replace(token, "")
    cleaned_value = cleaned_value.replace(" ", "")
    filtered_value = "".join(character for character in cleaned_value if character.isdigit() or character in ",.-")
    if not filtered_value:
        return None

    if "," in filtered_value and "." in filtered_value:
        filtered_value = filtered_value.replace(",", "")
    elif "," in filtered_value:
        filtered_value = filtered_value.replace(",", ".")

    try:
        return Decimal(filtered_value)
    except (InvalidOperation, ValueError):
        return None


def _request_pabilo_verify(url: str, api_key: str, payload: dict[str, Any], timeout: int):
    response, data = _request_pabilo_json("POST", url, api_key, timeout, payload)
    if response is None:
        return None, {
            "ok": False,
            "found": False,
            "verified": False,
            "message": data.get("message") or "No se pudo consultar Pabilo.",
        }
    return response, data


def _request_pabilo_json(method: str, url: str, api_key: str, timeout: int, payload: Optional[dict[str, Any]] = None):
    try:
        response = requests.request(
            method=method,
            url=url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "appKey": api_key,
            },
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        return None, {"ok": False, "message": "Pabilo no respondió a tiempo."}
    except requests.exceptions.ConnectionError:
        return None, {"ok": False, "message": "No se pudo conectar con Pabilo."}
    except Exception as exc:
        return None, {"ok": False, "message": f"Error consultando Pabilo: {exc}"}

    try:
        data = response.json()
    except Exception:
        data = {}
    return response, data


def _extract_pabilo_payload(data: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(data, dict):
        return {}, {}

    inner_data = data.get("data")
    if isinstance(inner_data, dict):
        return inner_data, data

    return data, data


def _is_rate_limited_response(status_code: int, data: dict[str, Any]) -> bool:
    if status_code == 429:
        return True

    message = f"{data.get('message') or ''} {data.get('error') or ''}".strip().lower()
    if not message:
        return False

    return any(
        fragment in message
        for fragment in (
            "too many requests",
            "[429]",
            "servicio no disponible",
            "intente más tarde",
            "cannot unmarshal object into go value of type mooc.accountmovements",
        )
    )


def _normalize_payment_data(reference: str, payload_data: dict[str, Any], full_data: dict[str, Any]) -> dict[str, Any]:
    payment_data = payload_data.get("user_bank_payment") or payload_data.get("payment") or payload_data
    status_value = str(payment_data.get("status") or payload_data.get("status") or full_data.get("status") or "").strip().lower()
    verification_id = str(payment_data.get("id") or payload_data.get("id") or "").strip() or None
    raw_amount = (
        payment_data.get("amount")
        or payment_data.get("payment_amount")
        or payment_data.get("amount_bs")
        or payment_data.get("amountBs")
        or payload_data.get("amount")
        or full_data.get("amount")
    )
    amount_decimal = _coerce_decimal_amount(raw_amount)
    verified_flag = bool(payload_data.get("verified") or full_data.get("verified"))
    is_verified = status_value in PABILO_ACCEPTED_STATUSES or verified_flag
    normalized_reference = str(
        payment_data.get("bank_reference")
        or payment_data.get("bank_reference_id")
        or payment_data.get("reference")
        or payload_data.get("bank_reference")
        or reference
    ).strip()

    return {
        "reference": normalized_reference,
        "status": status_value or "desconocido",
        "verification_id": verification_id,
        "amount_paid_value": f"{amount_decimal:.2f}" if amount_decimal is not None else None,
        "amount_paid_currency": "BS" if amount_decimal is not None else None,
        "verified": is_verified,
        "is_new": bool(payload_data.get("is_new")),
        "raw": payment_data,
    }


def _normalize_required_fields(fields_required: Any) -> list[str]:
    normalized_fields: list[str] = []
    for field in fields_required or []:
        if isinstance(field, dict):
            field_name = str(field.get("name") or "").strip().upper()
        else:
            field_name = str(field or "").strip().upper()
        if field_name:
            normalized_fields.append(field_name)
    return normalized_fields


def _get_user_bank_configuration(api_key: str, user_bank_id: str, base_url: str, timeout: int) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    url = f"{base_url.rstrip('/')}/me/usersbank"
    response, data = _request_pabilo_json("GET", url, api_key, timeout)
    if response is None:
        return None, data
    if response.status_code == 401:
        return None, {"ok": False, "message": "La API key de Pabilo es inválida o está inactiva.", "response": data}
    if response.status_code >= 400:
        return None, {
            "ok": False,
            "message": data.get("message") or data.get("error") or f"Pabilo devolvió HTTP {response.status_code} al consultar las cuentas.",
            "response": data,
        }

    user_banks = data.get("user_banks") or []
    selected_bank = next((bank for bank in user_banks if str(bank.get("id") or "").strip() == user_bank_id), None)
    if not selected_bank:
        return None, {
            "ok": False,
            "message": "El UserBankId configurado no aparece entre las cuentas disponibles de Pabilo.",
            "response": data,
        }
    return selected_bank, None


def _select_reference_only_verification(user_bank: dict[str, Any]) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    available_types: list[dict[str, Any]] = []
    for verification_type in user_bank.get("verifications_types_available") or []:
        verification_id = str(verification_type.get("id") or "").strip().upper()
        required_fields = _normalize_required_fields(verification_type.get("fields_required"))
        available_types.append({
            "id": verification_id,
            "required_fields": required_fields,
        })
        if set(required_fields).issubset(REFERENCE_ONLY_FIELD_NAMES):
            return {
                "id": verification_id or PABILO_DEFAULT_MOVEMENT_TYPE,
                "required_fields": required_fields,
            }, available_types
    return None, available_types


def verify_pabilo_reference(
    *,
    api_key: str,
    user_bank_id: str,
    reference: str,
    base_url: str,
    timeout: int,
) -> dict[str, Any]:
    normalized_reference = str(reference or "").strip()
    if not normalized_reference:
        return {"ok": False, "found": False, "verified": False, "message": "La referencia bancaria es obligatoria."}
    if not api_key:
        return {"ok": False, "found": False, "verified": False, "message": "Falta configurar la API key de Pabilo."}
    if not user_bank_id:
        return {"ok": False, "found": False, "verified": False, "message": "Falta configurar el UserBankId de Pabilo."}

    user_bank, configuration_error = _get_user_bank_configuration(api_key, user_bank_id, base_url, timeout)
    if configuration_error:
        return {
            "ok": False,
            "found": False,
            "verified": False,
            **configuration_error,
        }

    selected_verification, available_types = _select_reference_only_verification(user_bank or {})
    if not selected_verification:
        return {
            "ok": False,
            "found": False,
            "verified": False,
            "message": "La cuenta Pabilo configurada no permite verificación solo por referencia. Esta cuenta exige datos adicionales del pagador o un tipo de movimiento específico.",
            "available_verification_types": available_types,
            "provider": user_bank.get("provider") if user_bank else None,
        }

    payload = {
        "bank_reference": normalized_reference,
        "movement_type": selected_verification["id"],
    }
    url = f"{base_url.rstrip('/')}/userbankpayment/{user_bank_id}/betaserio"
    response, data = _request_pabilo_verify(url, api_key, payload, timeout)
    if response is None:
        return data

    payload_data, full_data = _extract_pabilo_payload(data)
    if response.status_code == 404:
        return {
            "ok": True,
            "found": False,
            "verified": False,
            "message": "El pago todavía no aparece verificado en Pabilo.",
            "response": full_data,
        }
    if response.status_code == 401:
        return {
            "ok": False,
            "found": False,
            "verified": False,
            "message": "La API key de Pabilo es inválida o está inactiva.",
            "response": full_data,
        }
    if response.status_code == 402:
        return {
            "ok": False,
            "found": False,
            "verified": False,
            "message": "La cuenta de Pabilo no tiene créditos suficientes.",
            "response": full_data,
        }
    if _is_rate_limited_response(response.status_code, data):
        return {
            "ok": True,
            "found": False,
            "verified": False,
            "rate_limited": True,
            "message": "Pabilo está recibiendo demasiadas solicitudes. Reintenta en unos segundos.",
            "response": full_data,
        }
    if response.status_code >= 400:
        return {
            "ok": False,
            "found": False,
            "verified": False,
            "message": full_data.get("message") or full_data.get("error") or f"Pabilo devolvió HTTP {response.status_code}.",
            "response": full_data,
        }

    payment = _normalize_payment_data(normalized_reference, payload_data, full_data)
    return {
        "ok": True,
        "found": True,
        "verified": payment["verified"],
        "message": "Pago encontrado correctamente en Pabilo." if payment["verified"] else "Pabilo devolvió la transacción, pero aún no figura como verificada.",
        "payment": payment,
        "response": full_data,
    }
