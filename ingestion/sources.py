"""Small, explicit official-source selections; not a population dataset."""

from pathlib import Path
from urllib.parse import urlsplit

from pydantic import TypeAdapter

from ingestion.contracts import SourceSpec

REGISTRY = Path(__file__).resolve().parents[1] / "examples/nhtsa-sources.json"


def load_sources() -> dict[str, SourceSpec]:
    sources = TypeAdapter(list[SourceSpec]).validate_json(REGISTRY.read_bytes())
    result = {}
    for source in sources:
        url = urlsplit(source.url)
        if url.scheme != "https" or url.hostname not in {"api.nhtsa.gov", "static.nhtsa.gov"}:
            raise ValueError("source registry requires an official HTTPS host")
        if url.username or url.password or url.port not in {None, 443} or url.fragment:
            raise ValueError("source registry contains unsupported URL components")
        if source.source_id in result:
            raise ValueError("duplicate source_id")
        result[source.source_id] = source
    return result
