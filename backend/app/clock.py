"""Relógio do turno: data/hora local e contas de calendário feitas em Python, nunca pelo modelo.

Medido no singleagent (ADR-007 do vault): sem a data no prompt o modelo não sabe que dia é e
não percebe uma previsão de entrega vencida; com a data, ainda erra conta de dias ("72 dias até
o Natal" em 13/10 — são 73). Por isso aqui o prompt recebe a data (bloco DINÂMICO, para não
invalidar o prompt cache do estático) e toda distância entre datas chega pronta no documento.
"""

import calendar
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo(os.getenv("AGENT_TIMEZONE", "America/Sao_Paulo"))
_WEEKDAYS = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
             "sexta-feira", "sábado", "domingo")
_MONTHS = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
           "agosto", "setembro", "outubro", "novembro", "dezembro")


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def today_label(now: datetime | None = None) -> str:
    now = now or local_now()
    return f"{_WEEKDAYS[now.weekday()]}, {now.day} de {_MONTHS[now.month - 1]} de {now.year}"


def clock_block(now: datetime | None = None) -> str:
    """Uma linha para o contexto dinâmico. Curta de propósito: entra em todo prompt de agente."""
    now = now or local_now()
    return (f"Agora: {today_label(now)}, {now:%H:%M} ({LOCAL_TZ.key}). Prazos e dias que faltam já "
            "vêm calculados nos documentos: use esses campos, não conte de cabeça.")


def parse_day(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def add_months(day: date, months: int) -> date:
    """Mês de calendário (12 meses = 1 ano), não `months * 30` dias. Dia 31 vira o último do mês."""
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def days_from_today(target, now: datetime | None = None) -> int | None:
    """Positivo = no futuro; negativo = já passou; None = data ilegível."""
    day = parse_day(target)
    if day is None:
        return None
    return (day - (now or local_now()).date()).days


def uses_current_date(answer: str, now: datetime | None = None) -> bool:
    """A resposta cita a data de HOJE → depende de quando foi perguntada → não entra no cache.

    Regra pela resposta, não por lista de palavras da pergunta. Falso positivo só custa um miss.
    """
    now = now or local_now()
    text = " ".join((answer or "").lower().split())
    forms = (f"{now:%d/%m/%Y}", f"{now.day}/{now.month}/{now.year}", f"{now:%d/%m}", f"{now:%Y-%m-%d}",
             f"{now.day} de {_MONTHS[now.month - 1]}", f"{now.day:02d} de {_MONTHS[now.month - 1]}")
    return any(form in text for form in forms)
