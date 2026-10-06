"""Máscara de PII no caminho principal (app/security.py) contra formatos e ofuscações reais."""

import pytest

from app.guardrails import _is_pii_only
from app.security import mask_pii


@pytest.mark.parametrize("raw, label", [
    ("meu cpf 529.982.247-25", "[CPF]"),
    ("cpf 529.​982.247-25", "[CPF]"),            # zero-width no meio
    ("cpf ５２９.９８２.２４７-２５", "[CPF]"),          # dígitos full-width
    ("cnpj 11.222.333/0001-81", "[CNPJ]"),            # vazava inteiro antes
    ("cartão 4111 1111 1111 1111", "[CARTAO]"),       # antes virava dois [TELEFONE]
    ("cartão 4111-1111-1111-1111", "[CARTAO]"),
    ("email a.b+c@ex.com.br", "[EMAIL]"),
    ("tel +55 11 98765-4321", "[TELEFONE]"),
    ("tel (11) 98765-4321", "[TELEFONE]"),
])
def test_pii_is_masked(raw, label):
    masked = mask_pii(raw)
    assert label in masked
    assert not any(ch.isdigit() for ch in masked.split(label)[0][-3:]), masked


@pytest.mark.parametrize("raw", ["cpf 52998224725", "cnpj 11222333000181"])
def test_unformatted_documents_never_leak_even_with_a_generic_label(raw):
    assert not any(ch.isdigit() for ch in mask_pii(raw))


@pytest.mark.parametrize("business", ["pedido PED-1001 e fatura FAT-1001", "CEP 01310-100", "R$ 1.299,90 em 12x",
                                      "chamado TCK-BRUNO-60747"])
def test_business_identifiers_are_not_masked(business):
    assert mask_pii(business) == business


def test_pasted_own_documents_are_not_treated_as_an_attack():
    assert _is_pii_only(mask_pii("meu cnpj 11.222.333/0001-81 e cartao 4111 1111 1111 1111").lower())
