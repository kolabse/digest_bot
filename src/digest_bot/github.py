from __future__ import annotations

import math
import os
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlparse

import httpx

from .errors import DeliveryOperationError

REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class RepositorySourceError(DeliveryOperationError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(
            message,
            retryable=retryable,
            retry_after_seconds=retry_after_seconds,
        )


GitHubSourceError = RepositorySourceError


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After", "").strip()
    try:
        seconds = float(value) if value else None
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at - datetime.now(UTC)).total_seconds()
    if seconds is None or not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds


def _raise_for_api_status(response: httpx.Response, provider: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        retryable = response.status_code in {408, 425, 429} or response.status_code >= 500
        raise RepositorySourceError(
            f"{provider} API вернул HTTP {response.status_code}.",
            retryable=retryable,
            retry_after_seconds=_retry_after_seconds(response) if retryable else None,
        ) from exc


def normalize_repository(
    value: str, allowed_gitlab_hosts: frozenset[str] = frozenset()
) -> str:
    candidate = value.strip().removesuffix(".git").rstrip("/")
    if "://" in candidate:
        parsed = urlparse(candidate)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ValueError("Используйте HTTPS URL репозитория без логина и токена.")
        project_path = parsed.path.strip("/").removesuffix(".git")
        if hostname in {"github.com", "www.github.com"}:
            candidate = project_path
        elif hostname in allowed_gitlab_hosts:
            if len(project_path.split("/")) < 2:
                raise ValueError("URL GitLab должен содержать группу и проект.")
            return f"{parsed.scheme}://{parsed.netloc}/{project_path}"
        else:
            raise ValueError(
                "Допустимы github.com и GitLab-инстансы из GITLAB_ALLOWED_HOSTS."
            )
    if not REPOSITORY.fullmatch(candidate):
        raise ValueError("Укажите репозиторий как owner/name или URL на github.com.")
    return candidate


def normalize_digest_path(value: str) -> str:
    candidate = value.strip().lstrip("/")
    parts = candidate.split("/")
    if not candidate or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Укажите путь к файлу внутри репозитория без '..'.")
    return candidate


class RepositoryContentsSource:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        allowed_gitlab_hosts: frozenset[str] = frozenset(),
    ) -> None:
        self._allowed_gitlab_hosts = allowed_gitlab_hosts
        self._client = client or httpx.AsyncClient(timeout=20)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def read(
        self,
        repository: str,
        path: str,
        ref: str,
        token_env: str | None,
    ) -> str:
        token = os.getenv(token_env, "").strip() if token_env else ""
        if token_env and not token:
            raise GitHubSourceError(f"Переменная окружения {token_env} не задана.")
        if "://" in repository:
            return await self._read_gitlab(repository, path, ref, token)
        return await self._read_github(repository, path, ref, token)

    async def _read_github(self, repository: str, path: str, ref: str, token: str) -> str:
        encoded_path = "/".join(quote(part, safe="") for part in path.split("/"))
        headers = {
            "Accept": "application/vnd.github.raw+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "project-digest-bot",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await self._client.get(
                f"https://api.github.com/repos/{repository}/contents/{encoded_path}",
                params={"ref": ref},
                headers=headers,
            )
        except httpx.TransportError as exc:
            raise RepositorySourceError(
                "Не удалось подключиться к GitHub API.", retryable=True
            ) from exc
        if response.status_code == 404:
            raise RepositorySourceError(
                "Репозиторий или файл не найден. Для приватного репозитория "
                "проверьте токен и его права."
            )
        if response.status_code == 403 and (
            response.headers.get("X-RateLimit-Remaining") == "0"
            or response.headers.get("Retry-After")
        ):
            raise RepositorySourceError(
                "GitHub временно ограничил частоту запросов.",
                retryable=True,
                retry_after_seconds=_retry_after_seconds(response),
            )
        if response.status_code in {401, 403}:
            raise RepositorySourceError(
                "GitHub отклонил доступ. Проверьте токен и лимиты API."
            )
        _raise_for_api_status(response, "GitHub")
        return response.text

    async def _read_gitlab(
        self, repository: str, path: str, ref: str, token: str
    ) -> str:
        parsed = urlparse(repository)
        hostname = (parsed.hostname or "").lower()
        if hostname not in self._allowed_gitlab_hosts:
            raise RepositorySourceError("GitLab-инстанс отсутствует в allowlist.")
        project = quote(parsed.path.strip("/").removesuffix(".git"), safe="")
        file_path = quote(path, safe="")
        headers = {"User-Agent": "project-digest-bot"}
        if token:
            headers["PRIVATE-TOKEN"] = token
        try:
            response = await self._client.get(
                f"{parsed.scheme}://{parsed.netloc}/api/v4/projects/{project}/repository/files/"
                f"{file_path}/raw",
                params={"ref": ref},
                headers=headers,
            )
        except httpx.TransportError as exc:
            raise RepositorySourceError(
                "Не удалось подключиться к GitLab API.", retryable=True
            ) from exc
        if response.status_code == 404:
            raise RepositorySourceError(
                "Проект или файл GitLab не найден. Для приватного проекта проверьте "
                "токен, ref и путь."
            )
        if response.status_code in {401, 403}:
            raise RepositorySourceError(
                "GitLab отклонил доступ. Токену нужен read_repository или read_api."
            )
        _raise_for_api_status(response, "GitLab")
        return response.text


GitHubContentsSource = RepositoryContentsSource
