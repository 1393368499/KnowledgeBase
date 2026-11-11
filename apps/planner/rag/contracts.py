"""Versioned evidence contract shared by retrieval, reading and verification."""
from dataclasses import asdict, dataclass
from hashlib import sha256
from urllib.parse import urlencode


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    file_id: int
    version: str
    ordinal: int
    page: int | None
    name: str
    library: str
    text: str
    text_hash: str
    source: str
    truncated: bool = False

    @classmethod
    def from_chunk(cls, chunk, *, source="reader", text=None, truncated=False):
        record = chunk.file
        digest = sha256(chunk.text.encode("utf-8")).hexdigest()
        version = record.content_sha256
        key = sha256(f"{record.pk}:{version}:{chunk.ordinal}:{digest}".encode()).hexdigest()[:20]
        return cls(f"E{key}", record.pk, version, chunk.ordinal, chunk.page,
                   record.original_name, record.library.code,
                   chunk.text if text is None else text, digest, source, truncated)

    def to_dict(self):
        result = asdict(self)
        result["url"] = f"/api/v1/planner/evidence/{self.file_id}/?" + urlencode({
            "ordinal": self.ordinal, "version": self.version, "text_hash": self.text_hash,
        })
        return result
