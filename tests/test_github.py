import httpx
import pytest

from digest_bot.github import GitHubContentsSource, GitHubSourceError, normalize_repository


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("kolabse/docs", "kolabse/docs"),
        ("https://github.com/kolabse/docs.git", "kolabse/docs"),
    ],
)
def test_normalize_repository(value: str, expected: str) -> None:
    assert normalize_repository(value) == expected


def test_rejects_non_github_url() -> None:
    with pytest.raises(ValueError):
        normalize_repository("https://example.com/owner/repo")


def test_accepts_allowlisted_self_hosted_gitlab() -> None:
    repository = normalize_repository(
        "https://gitlab.product.fproject.ru/fabrikum/portal-docs",
        frozenset({"gitlab.product.fproject.ru"}),
    )
    assert repository == "https://gitlab.product.fproject.ru/fabrikum/portal-docs"


def test_rejects_non_allowlisted_gitlab() -> None:
    with pytest.raises(ValueError, match="GITLAB_ALLOWED_HOSTS"):
        normalize_repository("https://gitlab.internal/owner/repo")


async def test_reads_raw_contents() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"] == "application/vnd.github.raw+json"
        assert request.url.params["ref"] == "main"
        return httpx.Response(200, text="# Дайджест проекта")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = GitHubContentsSource(client)
    content = await source.read("owner/repo", "docs/project-digest.md", "main", None)
    assert content == "# Дайджест проекта"
    await client.aclose()


async def test_private_repository_error_is_actionable() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    source = GitHubContentsSource(client)
    with pytest.raises(GitHubSourceError, match="приватного"):
        await source.read("owner/repo", "docs/project-digest.md", "main", None)
    await client.aclose()


async def test_reads_private_gitlab_raw_file(monkeypatch) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "secret")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "gitlab.product.fproject.ru"
        assert "fabrikum%2Fportal-docs" in str(request.url)
        assert "docs%2Fproject-digest.md" in str(request.url)
        assert request.headers["private-token"] == "secret"
        return httpx.Response(200, text="# Дайджест проекта")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = GitHubContentsSource(
        client,
        allowed_gitlab_hosts=frozenset({"gitlab.product.fproject.ru"}),
    )
    content = await source.read(
        "https://gitlab.product.fproject.ru/fabrikum/portal-docs",
        "docs/project-digest.md",
        "main",
        "GITLAB_TOKEN",
    )
    assert content == "# Дайджест проекта"
    await client.aclose()
