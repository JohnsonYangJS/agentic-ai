from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ContentValidationError(ValueError):
    pass


@dataclass(frozen=True)
class XhsContent:
    title: str
    body: str
    topics: list[str]
    images: list[Path]
    mode: str
    source_path: Path

    @property
    def body_with_topics(self) -> str:
        tags = " ".join(f"#{topic}" for topic in self.topics)
        if not tags:
            return self.body
        return f"{self.body.rstrip()}\n\n{tags}"

    @property
    def fingerprint(self) -> str:
        h = hashlib.sha256()
        h.update(self.title.encode("utf-8"))
        h.update(b"\0")
        h.update(self.body.encode("utf-8"))
        for image in self.images:
            h.update(b"\0")
            h.update(str(image.resolve()).encode("utf-8"))
            if image.exists():
                h.update(str(image.stat().st_size).encode("utf-8"))
        return h.hexdigest()

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "body": self.body,
            "topics": self.topics,
            "images": [str(path) for path in self.images],
            "mode": self.mode,
            "fingerprint": self.fingerprint,
            "source_path": str(self.source_path),
        }


def load_content(path: Path, *, mode_override: str | None = None) -> XhsContent:
    if not path.exists():
        raise ContentValidationError(f"content file does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ContentValidationError("content JSON root must be an object")

    title = str(payload.get("title") or "").strip()
    body = str(payload.get("body") or "").strip()
    topics = payload.get("topics") or []
    images = payload.get("images") or []
    mode = str(mode_override or payload.get("mode") or "draft").strip().lower()

    if not title:
        raise ContentValidationError("title is required")
    if len(title) > 80:
        raise ContentValidationError("title is too long; keep it within 80 chars")
    if not body:
        raise ContentValidationError("body is required")
    if len(body) > 2000:
        raise ContentValidationError("body is too long; keep it within 2000 chars")
    if mode not in {"draft", "publish"}:
        raise ContentValidationError("mode must be draft or publish")
    if not isinstance(topics, list) or any(not str(topic).strip() for topic in topics):
        raise ContentValidationError("topics must be a list of non-empty strings")
    if not isinstance(images, list):
        raise ContentValidationError("images must be a list")
    if mode == "publish" and not images:
        raise ContentValidationError("publish mode requires at least one image")

    base_dir = path.parent
    image_paths: list[Path] = []
    for raw in images:
        image_path = Path(str(raw))
        if not image_path.is_absolute():
            image_path = (base_dir / image_path).resolve()
        if not image_path.exists():
            raise ContentValidationError(f"image does not exist: {image_path}")
        if image_path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise ContentValidationError(f"unsupported image type: {image_path}")
        image_paths.append(image_path)

    clean_topics = [str(topic).strip().lstrip("#") for topic in topics]
    return XhsContent(
        title=title,
        body=body,
        topics=clean_topics,
        images=image_paths,
        mode=mode,
        source_path=path,
    )
