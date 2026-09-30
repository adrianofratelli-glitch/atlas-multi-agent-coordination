from copy import copy

import pytest

from app.config import Settings
from app.main import validate_runtime_security


def production_settings() -> Settings:
    return Settings(
        ENVIRONMENT="production",
        jwt_secret="j" * 32,
        admin_api_key="a" * 24,
        auth_required=True,
        demo_token_issuance_enabled=False,
        cors_origins="https://pov.example.com",
    )


def test_secure_production_configuration_is_accepted():
    validate_runtime_security(production_settings())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("auth_required", False),
        ("demo_token_issuance_enabled", True),
        ("cors_origins", "*"),
        ("jwt_secret", "short"),
        ("admin_api_key", "short"),
    ],
)
def test_insecure_production_configuration_is_rejected(field, value):
    settings = copy(production_settings())
    setattr(settings, field, value)
    with pytest.raises(RuntimeError):
        validate_runtime_security(settings)
