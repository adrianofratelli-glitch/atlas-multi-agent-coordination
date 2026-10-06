import re
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings, get_settings
from .database import DataStore, get_store


bearer = HTTPBearer(auto_error=False)


def issue_token(customer_key: str, settings: Settings) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": customer_key,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_ttl_minutes),
        "iss": settings.app_name,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


async def current_customer(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[DataStore, Depends(get_store)],
) -> dict:
    if not credentials:
        if settings.auth_required:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token obrigatório")
        fallback = await store.find_one("customers", {"customer_key": "ana"})
        if fallback:
            return fallback
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "seed não executado")
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret,
            algorithms=["HS256"],
            issuer=settings.app_name,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token inválido") from exc
    customer = await store.find_one("customers", {"customer_key": payload["sub"]})
    if not customer:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "identidade desconhecida")
    return customer


def require_admin(
    x_admin_key: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    if not x_admin_key or not secrets_equal(x_admin_key, settings.admin_api_key):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "chave administrativa inválida")


def secrets_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode(), right.encode())


# Máscara própria, e não `guardrails.mask_pii` do pacote comum, por dois motivos medidos
# (2026-10-06): o do pacote não mascara cartão sem Presidio, e o formato `[LABEL]` é contrato de
# `guardrails._is_pii_only` (frase só de PII não envenena o denylist). Ordem importa: CNPJ antes de
# CPF; telefone não pode começar/terminar colado em outro grupo de dígitos (senão um cartão
# "4111 1111 1111 1111" virava dois [TELEFONE]).
PII_PATTERNS = [
    (re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"(?<!\d)\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}(?!\d)"), "[CNPJ]"),
    (re.compile(r"(?<!\d)\d{3}\.\d{3}\.\d{3}-\d{2}(?!\d)"), "[CPF]"),
    (re.compile(r"(?<!\d)(?<!\d )(?<!\d-)(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?9?\d{4}[-\s]?\d{4}(?![ -]?\d)"), "[TELEFONE]"),
    (re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"), "[CARTAO]"),
]
# Zero-width/bidi (categoria Unicode Cf) quebravam o casamento: "529.\u200b982.247-25" passava inteiro.
_FORMAT_CHARS = re.compile(r"[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


def mask_pii(text: str) -> str:
    masked = _FORMAT_CHARS.sub("", text)
    for pattern, replacement in PII_PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked


def request_identity_key(request: Request, customer_key: str) -> str:
    ip = request.client.host if request.client else "unknown"
    return f"{ip}:{customer_key}"

