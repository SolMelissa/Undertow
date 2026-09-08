"""
Hydrus Client API layer for tag_cleanup_x.
"""

import json
import sys
import time
from typing import Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    print("This tool requires the 'requests' package: pip install requests", file=sys.stderr)
    sys.exit(1)


class HydrusClient:
    def __init__(self, base_url: str, access_key: str, retries: int = 3, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Hydrus-Client-API-Access-Key"] = access_key
        self.retries = retries
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> dict:
        return self._request("GET", path, params=params)

    def _post(self, path: str, payload: dict) -> dict:
        return self._request("POST", path, json=payload)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
                resp.raise_for_status()
                return resp.json() if resp.content else {}
            except Exception as exc:  # noqa: BLE001 - want to retry on any transient failure
                last_exc = exc
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 10))
        raise RuntimeError(f"Request to {url} failed after {self.retries} attempts: {last_exc}")

    def get_services(self) -> dict:
        return self._get("/get_services", {})

    def resolve_service_key(self, name: str) -> str:
        services = self.get_services()
        services_dict = services.get("services", {})
        if isinstance(services_dict, dict):
            for key, svc in services_dict.items():
                if svc.get("name") == name:
                    return key
        available = sorted(svc.get("name", "?") for svc in services_dict.values()) if isinstance(services_dict, dict) else []
        raise ValueError(
            f"No Hydrus service found named {name!r}. Available services: {', '.join(available) or '(none returned)'}"
        )

    def list_services(self) -> List[Tuple[str, str, str]]:
        """Returns (name, service_key, type_pretty) tuples for every service Hydrus knows about."""
        services = self.get_services()
        services_dict = services.get("services", {})
        if not isinstance(services_dict, dict):
            return []
        return sorted(
            (svc.get("name", "?"), key, svc.get("type_pretty", "?"))
            for key, svc in services_dict.items()
        )

    def list_file_services(self) -> List[Tuple[str, str, str]]:
        # "all known files" is a virtual combined domain - Hydrus's search_files endpoint
        # rejects it outright with a 400 (it's not a concrete, searchable file domain, unlike
        # "all local files"/"all my files"/real import services), so don't offer it as a choice.
        return [s for s in self.list_services() if "file" in s[2].lower() and s[0] != "all known files"]

    def list_tag_services(self) -> List[Tuple[str, str, str]]:
        # "all known tags" is a virtual combined domain, same story as "all known files" above -
        # Hydrus's add_tags endpoint 400s if you try to add/delete tags on it directly, since it
        # isn't a real, writable tag service (unlike "my tags"/a real PTR/other real services).
        return [s for s in self.list_services() if "tag" in s[2].lower() and s[0] != "all known tags"]

    def search_files(self, tags: List[str], file_service_key: str) -> List[int]:
        params = {
            "tags": json.dumps(tags),
            "file_service_key": file_service_key,
            "return_file_ids": "true",
        }
        result = self._get("/get_files/search_files", params)
        return result.get("file_ids", [])

    def fetch_metadata(self, file_ids: List[int], tag_service_key: str,
                        chunk_size: int = 256, on_progress=None) -> Dict[int, List[str]]:
        """Chunked so a large library (tens of thousands of files) doesn't build one giant
        query string and time out in a single request; on_progress(done, total), if given, is
        called after each chunk so the caller can print progress."""
        out: Dict[int, List[str]] = {}
        total = len(file_ids)
        for start in range(0, total, chunk_size):
            chunk = file_ids[start:start + chunk_size]
            params = {"file_ids": json.dumps(chunk)}
            result = self._get("/get_files/file_metadata", params)
            for meta in result.get("metadata", []):
                fid = meta.get("file_id")
                tags_block = meta.get("tags", {}).get(tag_service_key, {})
                storage = tags_block.get("storage_tags", {})
                current = storage.get("0", [])
                out[fid] = current
            if on_progress:
                on_progress(min(start + chunk_size, total), total)
        return out

    def add_tags(self, file_ids: List[int], tag_service_key: str,
                 tags_to_add: List[str], tags_to_delete: List[str]) -> None:
        self.add_tags_multi(file_ids, {tag_service_key: (tags_to_add, tags_to_delete)})

    def add_tags_multi(self, file_ids: List[int],
                        service_actions: Dict[str, Tuple[List[str], List[str]]]) -> None:
        """service_actions maps tag_service_key -> (tags_to_add, tags_to_delete), so a single
        call can add to one tag service (e.g. the cleaned-tag destination) while deleting from a
        different one (e.g. the raw-filename-tag source), when those aren't the same service."""
        service_keys_to_actions_to_tags: Dict[str, Dict[str, List[str]]] = {}
        for tag_service_key, (tags_to_add, tags_to_delete) in service_actions.items():
            actions: Dict[str, List[str]] = {}
            if tags_to_add:
                actions["0"] = tags_to_add
            if tags_to_delete:
                actions["1"] = tags_to_delete
            if actions:
                service_keys_to_actions_to_tags[tag_service_key] = actions
        if not service_keys_to_actions_to_tags:
            return
        payload = {
            "file_ids": file_ids,
            "service_keys_to_actions_to_tags": service_keys_to_actions_to_tags,
        }
        self._post("/add_tags/add_tags", payload)
