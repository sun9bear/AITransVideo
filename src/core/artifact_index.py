from dataclasses import dataclass, field
from os import PathLike, fspath


@dataclass(slots=True)
class ArtifactIndex:
    """Central file-path registry for workflow artifacts."""

    _artifacts: dict[str, str] = field(default_factory=dict)

    def register(self, key: str, path: str | PathLike[str]) -> str:
        normalized_key = self._normalize_key(key)
        normalized_path = self._normalize_path(path)
        self._artifacts[normalized_key] = normalized_path
        return normalized_path

    def get(self, key: str) -> str | None:
        normalized_key = self._normalize_key(key)
        return self._artifacts.get(normalized_key)

    def require(self, key: str) -> str:
        normalized_key = self._normalize_key(key)
        if normalized_key not in self._artifacts:
            raise KeyError(f"Artifact not found: {normalized_key}")
        return self._artifacts[normalized_key]

    def to_dict(self) -> dict[str, str]:
        return dict(self._artifacts)

    @staticmethod
    def _normalize_key(key: str) -> str:
        normalized_key = key.strip()
        if not normalized_key:
            raise ValueError("Artifact key is required")
        return normalized_key

    @staticmethod
    def _normalize_path(path: str | PathLike[str]) -> str:
        normalized_path = fspath(path).strip()
        if not normalized_path:
            raise ValueError("Artifact path is required")
        return normalized_path
