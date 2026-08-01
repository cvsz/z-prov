"""Credential-isolated bounded provider control adapters."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr

from zeaz_control.batches import (
    BatchCounts,
    BatchPage,
    BatchRecord,
    BatchResult,
    BatchStatus,
    BatchSubmission,
)
from zeaz_control.files import FilePurpose, FileRecord
from zeaz_control.models import (
    DiscoveredModel,
    ModelLifecycle,
    ModelPage,
)


class ControlAdapterError(RuntimeError):
    """A sanitized provider control API failure."""


class _HTTPControlAdapter:
    def __init__(
        self,
        *,
        provider: str,
        account: str,
        region: str,
        base_url: str,
        credential: SecretStr | str,
        client: httpx.AsyncClient | None,
        timeout_seconds: float,
        max_response_bytes: int,
        clock: Callable[[], datetime] | None,
    ) -> None:
        _validate_base_url(base_url)
        if not 0 < timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be between 0 and 300")
        if not 1024 <= max_response_bytes <= 67_108_864:
            raise ValueError("max_response_bytes must be between 1 KiB and 64 MiB")
        self.provider = provider
        self.account = account
        self.region = region
        self._base_url = base_url.rstrip("/")
        self._credential = (
            credential if isinstance(credential, SecretStr) else SecretStr(credential)
        )
        self._client = client or httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=timeout_seconds,
        )
        self._owns_client = client is None
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._clock = clock or (lambda: datetime.now(UTC))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(
        self,
        path: str,
        *,
        headers: dict[str, str],
        params: dict[str, str | int] | None = None,
    ) -> dict[str, Any]:
        return await self._request_json(
            "GET",
            path,
            headers=headers,
            params=params,
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        params: dict[str, str | int] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, Any, str]] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            async with self._client.stream(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                params=params,
                data=data,
                files=files,
                json=json_body,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    raise ControlAdapterError("provider control API redirects are forbidden")
                if response.status_code < 200 or response.status_code >= 300:
                    raise ControlAdapterError(
                        f"provider control API failed with HTTP {response.status_code}"
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise ControlAdapterError(
                            "provider control API response exceeded its byte limit"
                        )
        except ControlAdapterError:
            raise
        except httpx.HTTPError as exc:
            raise ControlAdapterError("provider control API request failed") from exc
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ControlAdapterError("provider control API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ControlAdapterError("provider control API returned a non-object response")
        return value


class OpenAIModelAdapter(_HTTPControlAdapter):
    def __init__(
        self,
        credential: SecretStr | str,
        *,
        account: str = "default",
        region: str = "global",
        base_url: str = "https://api.openai.com",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30,
        max_response_bytes: int = 4_194_304,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            provider="openai",
            account=account,
            region=region,
            base_url=base_url,
            credential=credential,
            client=client,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            clock=clock,
        )

    async def list_models(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> ModelPage:
        if cursor is not None:
            raise ControlAdapterError("OpenAI Models API does not support a cursor")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        value = await self._get(
            "/v1/models",
            headers={
                "authorization": f"Bearer {self._credential.get_secret_value()}",
                "accept": "application/json",
            },
        )
        raw_items = value.get("data")
        if not isinstance(raw_items, list) or len(raw_items) > 1000:
            raise ControlAdapterError("OpenAI Models API returned an invalid model list")
        observed = self._clock()
        items = tuple(
            _openai_model(
                raw,
                account=self.account,
                region=self.region,
                observed_at=observed,
            )
            for raw in raw_items
        )
        return ModelPage(items=items)

    async def upload_file(
        self,
        path,
        *,
        filename: str,
        media_type: str,
        purpose: FilePurpose,
        sha256: str,
    ) -> FileRecord:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise ControlAdapterError("validated upload is unavailable") from exc
        stream = os.fdopen(fd, "rb")
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ControlAdapterError("validated upload is not a regular file")
            value = await self._request_json(
                "POST",
                "/v1/files",
                headers={
                    "authorization": f"Bearer {self._credential.get_secret_value()}",
                    "accept": "application/json",
                },
                data={"purpose": _openai_purpose(purpose)},
                files={"file": (filename, stream, media_type)},
            )
        finally:
            stream.close()
        return _openai_file(
            value,
            account=self.account,
            expected_filename=filename,
            expected_bytes=info.st_size,
            expected_sha256=sha256,
            expected_media_type=media_type,
            expected_purpose=purpose,
        )

    async def download_file(self, file_id: str):
        encoded_id = quote(file_id, safe="")
        try:
            async with self._client.stream(
                "GET",
                f"{self._base_url}/v1/files/{encoded_id}/content",
                headers={
                    "authorization": f"Bearer {self._credential.get_secret_value()}",
                    "accept": "application/octet-stream",
                },
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    raise ControlAdapterError("provider file redirects are forbidden")
                if response.status_code < 200 or response.status_code >= 300:
                    raise ControlAdapterError(
                        f"provider file download failed with HTTP {response.status_code}"
                    )
                async for chunk in response.aiter_bytes():
                    if chunk:
                        yield chunk
        except ControlAdapterError:
            raise
        except httpx.HTTPError as exc:
            raise ControlAdapterError("provider file download failed") from exc

    async def delete_file(self, file_id: str) -> None:
        value = await self._request_json(
            "DELETE",
            f"/v1/files/{quote(file_id, safe='')}",
            headers={
                "authorization": f"Bearer {self._credential.get_secret_value()}",
                "accept": "application/json",
            },
        )
        if value.get("id") != file_id or value.get("deleted") is not True:
            raise ControlAdapterError("provider did not confirm file deletion")

    async def submit_batch(
        self,
        submission: BatchSubmission,
        *,
        idempotency_key: str,
    ) -> BatchRecord:
        if submission.provider != self.provider or submission.account != self.account:
            raise ControlAdapterError("batch submission scope does not match adapter")
        if submission.endpoint == "/v1/messages":
            raise ControlAdapterError("OpenAI does not support the Messages batch endpoint")
        value = await self._request_json(
            "POST",
            "/v1/batches",
            headers={
                "authorization": f"Bearer {self._credential.get_secret_value()}",
                "content-type": "application/json",
                "idempotency-key": idempotency_key,
            },
            json_body={
                "input_file_id": submission.input_file_id,
                "endpoint": submission.endpoint,
                "completion_window": submission.completion_window,
                "metadata": submission.metadata,
            },
        )
        return _openai_batch(value, account=self.account)

    async def list_batches(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> BatchPage:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        params: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            params["after"] = cursor
        value = await self._get(
            "/v1/batches",
            headers={
                "authorization": f"Bearer {self._credential.get_secret_value()}",
                "accept": "application/json",
            },
            params=params,
        )
        raw = value.get("data")
        if not isinstance(raw, list) or len(raw) > limit:
            raise ControlAdapterError("OpenAI Batch API returned an invalid list")
        items = tuple(_openai_batch(item, account=self.account) for item in raw)
        has_more = value.get("has_more", False)
        if not isinstance(has_more, bool):
            raise ControlAdapterError("OpenAI Batch API returned invalid pagination")
        next_cursor = value.get("last_id") if has_more else None
        if has_more and (not isinstance(next_cursor, str) or not next_cursor):
            raise ControlAdapterError("OpenAI Batch API omitted its next cursor")
        return BatchPage(items=items, next_cursor=next_cursor)

    async def get_batch(self, batch_id: str) -> BatchRecord:
        value = await self._get(
            f"/v1/batches/{quote(batch_id, safe='')}",
            headers={
                "authorization": f"Bearer {self._credential.get_secret_value()}",
                "accept": "application/json",
            },
        )
        return _openai_batch(value, account=self.account)

    async def cancel_batch(
        self,
        batch_id: str,
        *,
        idempotency_key: str,
    ) -> BatchRecord:
        value = await self._request_json(
            "POST",
            f"/v1/batches/{quote(batch_id, safe='')}/cancel",
            headers={
                "authorization": f"Bearer {self._credential.get_secret_value()}",
                "content-type": "application/json",
                "idempotency-key": idempotency_key,
            },
            json_body={},
        )
        return _openai_batch(value, account=self.account)

    async def batch_results(self, batch: BatchRecord):
        if batch.provider != self.provider or batch.account != self.account:
            raise ControlAdapterError("batch result scope does not match adapter")
        if batch.output_file_id is None:
            raise ControlAdapterError("batch has no output file")
        buffer = bytearray()
        async for chunk in self.download_file(batch.output_file_id):
            buffer.extend(chunk)
            if len(buffer) > 16_777_216 and b"\n" not in buffer:
                raise ControlAdapterError("batch result line exceeded its byte limit")
            while (newline := buffer.find(b"\n")) >= 0:
                if newline > 16_777_216:
                    raise ControlAdapterError(
                        "batch result line exceeded its byte limit"
                    )
                line = bytes(buffer[:newline])
                del buffer[: newline + 1]
                if line:
                    yield _openai_batch_result(line, batch.id)
        if buffer:
            if len(buffer) > 16_777_216:
                raise ControlAdapterError("batch result line exceeded its byte limit")
            yield _openai_batch_result(bytes(buffer), batch.id)


class AnthropicModelAdapter(_HTTPControlAdapter):
    def __init__(
        self,
        credential: SecretStr | str,
        *,
        account: str = "default",
        region: str = "global",
        base_url: str = "https://api.anthropic.com",
        api_version: str = "2023-06-01",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30,
        max_response_bytes: int = 4_194_304,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not api_version or len(api_version) > 64:
            raise ValueError("api_version is invalid")
        super().__init__(
            provider="anthropic",
            account=account,
            region=region,
            base_url=base_url,
            credential=credential,
            client=client,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            clock=clock,
        )
        self._api_version = api_version

    async def list_models(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> ModelPage:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        params: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            params["after_id"] = cursor
        value = await self._get(
            "/v1/models",
            headers={
                "x-api-key": self._credential.get_secret_value(),
                "anthropic-version": self._api_version,
                "accept": "application/json",
            },
            params=params,
        )
        raw_items = value.get("data")
        if not isinstance(raw_items, list) or len(raw_items) > limit:
            raise ControlAdapterError("Anthropic Models API returned an invalid model list")
        observed = self._clock()
        items = tuple(
            _anthropic_model(
                raw,
                account=self.account,
                region=self.region,
                observed_at=observed,
            )
            for raw in raw_items
        )
        has_more = value.get("has_more", False)
        if not isinstance(has_more, bool):
            raise ControlAdapterError("Anthropic Models API returned invalid pagination")
        next_cursor = value.get("last_id") if has_more else None
        if has_more and (not isinstance(next_cursor, str) or not next_cursor):
            raise ControlAdapterError("Anthropic Models API omitted its next cursor")
        return ModelPage(items=items, next_cursor=next_cursor)


def _openai_model(
    value: Any,
    *,
    account: str,
    region: str,
    observed_at: datetime,
) -> DiscoveredModel:
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise ControlAdapterError("OpenAI Models API returned an invalid model")
    extensions: dict[str, Any] = {"openai": {}}
    for key in ("created", "owned_by", "object"):
        item = value.get(key)
        if isinstance(item, (str, int)):
            extensions["openai"][key] = item
    return DiscoveredModel(
        provider="openai",
        account=account,
        region=region,
        model=value["id"],
        lifecycle=_lifecycle(value.get("status")),
        source="openai_models_api",
        observed_at=observed_at,
        extensions=extensions,
    )


def _anthropic_model(
    value: Any,
    *,
    account: str,
    region: str,
    observed_at: datetime,
) -> DiscoveredModel:
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise ControlAdapterError("Anthropic Models API returned an invalid model")
    extensions: dict[str, Any] = {"anthropic": {}}
    for key in ("created_at", "display_name", "type"):
        item = value.get(key)
        if isinstance(item, (str, int)):
            extensions["anthropic"][key] = item
    return DiscoveredModel(
        provider="anthropic",
        account=account,
        region=region,
        model=value["id"],
        lifecycle=_lifecycle(value.get("status")),
        source="anthropic_models_api",
        observed_at=observed_at,
        extensions=extensions,
    )


def _openai_file(
    value: Any,
    *,
    account: str,
    expected_filename: str,
    expected_bytes: int,
    expected_sha256: str,
    expected_media_type: str,
    expected_purpose: FilePurpose,
) -> FileRecord:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("id"), str)
        or value.get("filename") != expected_filename
        or value.get("bytes") != expected_bytes
    ):
        raise ControlAdapterError("OpenAI Files API returned invalid upload metadata")
    created = value.get("created_at")
    if not isinstance(created, int) or created < 0:
        raise ControlAdapterError("OpenAI Files API returned an invalid creation time")
    remote_purpose = value.get("purpose")
    if remote_purpose != _openai_purpose(expected_purpose):
        raise ControlAdapterError("OpenAI Files API returned a mismatched purpose")
    extensions = {
        "openai": {
            key: item
            for key in ("object", "status", "expires_at")
            if isinstance((item := value.get(key)), (str, int))
        }
    }
    return FileRecord(
        provider="openai",
        account=account,
        id=value["id"],
        filename=expected_filename,
        bytes=expected_bytes,
        sha256=expected_sha256,
        media_type=expected_media_type,
        purpose=expected_purpose,
        created_at=datetime.fromtimestamp(created, tz=UTC),
        extensions=extensions,
    )


def _openai_batch(value: Any, *, account: str) -> BatchRecord:
    if not isinstance(value, dict):
        raise ControlAdapterError("OpenAI Batch API returned a non-object batch")
    required_strings = ("id", "input_file_id", "endpoint", "status")
    if any(not isinstance(value.get(key), str) for key in required_strings):
        raise ControlAdapterError("OpenAI Batch API returned invalid batch metadata")
    try:
        status = BatchStatus(value["status"])
    except ValueError as exc:
        raise ControlAdapterError("OpenAI Batch API returned an unknown status") from exc
    raw_counts = value.get("request_counts")
    if not isinstance(raw_counts, dict):
        raise ControlAdapterError("OpenAI Batch API returned invalid request counts")
    try:
        counts = BatchCounts(
            total=raw_counts.get("total"),
            completed=raw_counts.get("completed"),
            failed=raw_counts.get("failed"),
        )
    except Exception as exc:
        raise ControlAdapterError("OpenAI Batch API returned invalid request counts") from exc
    created_epoch = value.get("created_at")
    if not isinstance(created_epoch, int) or created_epoch < 0:
        raise ControlAdapterError("OpenAI Batch API returned invalid timestamps")
    timestamps = [
        item
        for key in (
            "in_progress_at",
            "finalizing_at",
            "completed_at",
            "failed_at",
            "expired_at",
            "cancelling_at",
            "cancelled_at",
        )
        if isinstance((item := value.get(key)), int) and item >= 0
    ]
    created_at = datetime.fromtimestamp(created_epoch, tz=UTC)
    updated_at = datetime.fromtimestamp(max([created_epoch, *timestamps]), tz=UTC)
    output_file_id = value.get("output_file_id")
    error_file_id = value.get("error_file_id")
    if output_file_id is not None and not isinstance(output_file_id, str):
        raise ControlAdapterError("OpenAI Batch API returned invalid output file metadata")
    if error_file_id is not None and not isinstance(error_file_id, str):
        raise ControlAdapterError("OpenAI Batch API returned invalid error file metadata")
    extensions = {
        "openai": {
            key: item
            for key in ("completion_window", "model", "metadata", "usage")
            if isinstance((item := value.get(key)), (str, dict))
        }
    }
    return BatchRecord(
        provider="openai",
        account=account,
        id=value["id"],
        input_file_id=value["input_file_id"],
        endpoint=value["endpoint"],
        status=status,
        counts=counts,
        output_file_id=output_file_id,
        error_file_id=error_file_id,
        created_at=created_at,
        updated_at=updated_at,
        extensions=extensions,
    )


def _openai_batch_result(line: bytes, batch_id: str) -> BatchResult:
    try:
        value = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlAdapterError("OpenAI batch output contains invalid JSONL") from exc
    if not isinstance(value, dict) or not isinstance(value.get("custom_id"), str):
        raise ControlAdapterError("OpenAI batch output contains an invalid result")
    response = value.get("response")
    error = value.get("error")
    if isinstance(response, dict) and error is None:
        status_code = response.get("status_code")
        body = response.get("body")
        try:
            return BatchResult(
                batch_id=batch_id,
                custom_id=value["custom_id"],
                status_code=status_code,
                response=body,
            )
        except Exception as exc:
            raise ControlAdapterError("OpenAI batch response is invalid") from exc
    if isinstance(error, dict) and response is None and isinstance(error.get("code"), str):
        return BatchResult(
            batch_id=batch_id,
            custom_id=value["custom_id"],
            error_code=error["code"],
        )
    raise ControlAdapterError("OpenAI batch result is ambiguous")


def _openai_purpose(value: FilePurpose) -> str:
    return {
        FilePurpose.BATCH: "batch",
        FilePurpose.FINE_TUNE: "fine-tune",
        FilePurpose.VISION: "vision",
        FilePurpose.ASSISTANTS: "assistants",
        FilePurpose.USER_DATA: "user_data",
        FilePurpose.EVALS: "evals",
    }[value]


def _lifecycle(value: Any) -> ModelLifecycle:
    try:
        return ModelLifecycle(str(value).lower())
    except ValueError:
        return ModelLifecycle.UNKNOWN


def _validate_base_url(value: str) -> None:
    parsed = urlsplit(value)
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("provider control base URL must use HTTPS unless loopback")
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("provider control base URL must be a credential-free origin")
