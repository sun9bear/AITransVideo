from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from core.models import SemanticBlock, SubtitleLine
from core.exceptions import StateError
from services.state_manager import utc_now_iso


class CacheManager:
    """Persist lightweight cache metadata and provide stable dedupe keys."""

    def __init__(self, cache_path: str) -> None:
        self.cache_path = Path(cache_path)

    def load(self) -> dict[str, Any]:
        if not self.cache_path.exists():
            return {"entries": {}, "metrics": {"hits": 0, "misses": 0}, "last_lookup": None}

        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"Failed to load cache file: {self.cache_path}") from exc
        return self._normalize_cache(data)

    def save(self, cache_data: dict[str, Any]) -> None:
        temp_path: Path | None = None
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            serialized_cache = json.dumps(cache_data, indent=2, sort_keys=True, ensure_ascii=False)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.cache_path.parent,
                prefix=f"{self.cache_path.stem}_",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_file.write(serialized_cache)
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path = Path(temp_file.name)
            os.replace(temp_path, self.cache_path)
        except OSError as exc:
            raise StateError(f"Failed to save cache file: {self.cache_path}") from exc
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def build_input_hash(self, lines: list[SubtitleLine]) -> str:
        payload = [
            {
                "index": line.index,
                "start_ms": line.start_ms,
                "end_ms": line.end_ms,
                "speaker_id": line.speaker_id,
                "speaker_name": line.speaker_name,
                "en_text": line.en_text,
                "cn_text": line.cn_text,
            }
            for line in lines
        ]
        return self._hash_payload({"kind": "subtitle_input", "lines": payload})

    def build_translation_batch_hash(
        self,
        lines: list[SubtitleLine],
        provider_name: str,
        target_language: str = "zh-CN",
        model_name: str | None = None,
        version_context: dict[str, Any] | None = None,
    ) -> str:
        payload = [
            {
                "index": line.index,
                "speaker_id": line.speaker_id,
                "en_text": line.en_text,
                "cn_text": line.cn_text,
            }
            for line in lines
        ]
        return self._hash_payload(
            {
                "kind": "translation_batch",
                "provider_name": provider_name,
                "model_name": model_name,
                "target_language": target_language,
                "version_context": version_context or {},
                "lines": payload,
            }
        )

    def build_tts_hash(
        self,
        block: SemanticBlock,
        provider_name: str,
        voice_name: str = "default",
        model_name: str | None = None,
        version_context: dict[str, Any] | None = None,
    ) -> str:
        selected_cn_text = block.get_preferred_cn_text_for_tts()
        payload = {
            "kind": "tts_block",
            "provider_name": provider_name,
            "voice_name": voice_name,
            "model_name": model_name,
            "version_context": version_context or {},
            "speaker_id": block.speaker_id,
            "speaker_name": block.speaker_name,
            "target_duration_ms": block.target_duration_ms,
            "selected_cn_text": selected_cn_text,
            "rewrite_count": block.rewrite_count,
        }
        return self._hash_payload(payload)

    def has_entry(self, namespace: str, cache_key: str) -> bool:
        """Probe cache presence without updating hit/miss metrics."""

        cache_data = self.load()
        return cache_key in cache_data["entries"].get(namespace, {})

    def get_entry(self, namespace: str, cache_key: str) -> dict[str, Any] | None:
        cache_data = self.load()
        namespace_entries = cache_data["entries"].setdefault(namespace, {})
        entry = namespace_entries.get(cache_key)
        timestamp = utc_now_iso()

        if entry is None:
            cache_data["metrics"]["misses"] += 1
            cache_data["last_lookup"] = {
                "namespace": namespace,
                "cache_key": cache_key,
                "result": "miss",
                "at": timestamp,
            }
            self.save(cache_data)
            return None

        entry["hit_count"] = int(entry.get("hit_count", 0)) + 1
        entry["last_accessed_at"] = timestamp
        cache_data["metrics"]["hits"] += 1
        cache_data["last_lookup"] = {
            "namespace": namespace,
            "cache_key": cache_key,
            "result": "hit",
            "at": timestamp,
        }
        self.save(cache_data)
        return entry

    def set_entry(self, namespace: str, cache_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        cache_data = self.load()
        namespace_entries = cache_data["entries"].setdefault(namespace, {})
        timestamp = utc_now_iso()
        existing_entry = namespace_entries.get(cache_key, {})
        namespace_entries[cache_key] = {
            "payload": payload,
            "created_at": existing_entry.get("created_at", timestamp),
            "updated_at": timestamp,
            "hit_count": existing_entry.get("hit_count", 0),
            "last_accessed_at": existing_entry.get("last_accessed_at"),
        }
        self.save(cache_data)
        return namespace_entries[cache_key]

    def _hash_payload(self, payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode("utf-8")).hexdigest()

    def _normalize_cache(self, cache_data: dict[str, Any]) -> dict[str, Any]:
        entries = cache_data.get("entries", {})
        normalized_entries: dict[str, Any] = {}
        for namespace, namespace_entries in entries.items():
            normalized_entries[namespace] = {}
            for cache_key, entry in namespace_entries.items():
                normalized_entries[namespace][cache_key] = {
                    "payload": entry.get("payload", {}),
                    "created_at": entry.get("created_at"),
                    "updated_at": entry.get("updated_at"),
                    "hit_count": entry.get("hit_count", 0),
                    "last_accessed_at": entry.get("last_accessed_at"),
                }
        return {
            "entries": normalized_entries,
            "metrics": {
                "hits": cache_data.get("metrics", {}).get("hits", 0),
                "misses": cache_data.get("metrics", {}).get("misses", 0),
            },
            "last_lookup": cache_data.get("last_lookup"),
        }
