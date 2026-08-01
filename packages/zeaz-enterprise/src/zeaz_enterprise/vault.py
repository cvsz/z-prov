"""Provider vault references that never contain secret material."""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
)

Name = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
]
Purpose = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AWSSecretReference(StrictModel):
    provider: Literal["aws-secrets-manager"] = "aws-secrets-manager"
    secret_arn: Annotated[str, StringConstraints(min_length=20, max_length=2048)]
    version_id: Annotated[
        str, StringConstraints(pattern=r"^[A-Za-z0-9-]{32,64}$")
    ] | None = None
    version_stage: Annotated[
        str, StringConstraints(pattern=r"^[A-Za-z0-9/_+=.@-]{1,256}$")
    ] | None = None

    @field_validator("secret_arn")
    @classmethod
    def complete_secret_arn(cls, value: str) -> str:
        parts = value.split(":")
        if (
            len(parts) < 7
            or parts[0] != "arn"
            or parts[2] != "secretsmanager"
            or not parts[3]
            or not parts[4].isdigit()
            or len(parts[4]) != 12
            or parts[5] != "secret"
            or not all(parts[6:])
        ):
            raise ValueError("a complete AWS Secrets Manager ARN is required")
        return value


class GCPSecretReference(StrictModel):
    provider: Literal["gcp-secret-manager"] = "gcp-secret-manager"
    project_id: Name
    secret_id: Name
    version: Annotated[
        str, StringConstraints(pattern=r"^(?:[1-9][0-9]{0,18}|latest|[A-Za-z][A-Za-z0-9_-]{0,254})$")
    ]

    @property
    def resource_name(self) -> str:
        return (
            f"projects/{self.project_id}/secrets/{self.secret_id}"
            f"/versions/{self.version}"
        )


class AzureSecretReference(StrictModel):
    provider: Literal["azure-key-vault"] = "azure-key-vault"
    secret_url: Annotated[str, StringConstraints(min_length=1, max_length=2048)]

    @field_validator("secret_url")
    @classmethod
    def versioned_secret_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        labels = (parsed.hostname or "").split(".")
        path = parsed.path.split("/")
        if (
            parsed.scheme != "https"
            or len(labels) != 4
            or labels[-3:] != ["vault", "azure", "net"]
            or not 3 <= len(labels[0]) <= 24
            or not labels[0].replace("-", "").isalnum()
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or len(path) != 4
            or path[1] != "secrets"
            or not 1 <= len(path[2]) <= 127
            or not path[2].replace("-", "").isalnum()
            or len(path[3]) != 32
            or not path[3].isalnum()
        ):
            raise ValueError("a versioned Azure Key Vault secret URL is required")
        return value


VaultReference = Annotated[
    AWSSecretReference | GCPSecretReference | AzureSecretReference,
    Field(discriminator="provider"),
]


class SecretBinding(StrictModel):
    """Persistable application state: purpose plus locator, never a value."""

    schema_version: Literal["1"] = "1"
    purpose: Purpose
    reference: VaultReference


_BINDING_ADAPTER = TypeAdapter(SecretBinding)


def parse_secret_binding(value: object) -> SecretBinding:
    """Validate an untrusted serialized binding with a closed-world schema."""

    return _BINDING_ADAPTER.validate_python(value)
