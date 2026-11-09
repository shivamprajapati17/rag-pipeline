import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200
def _chunks(text: str) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        if end < len(text):
            boundary = text.rfind(" ", start, end)
            if boundary > start:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end == len(text):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


def _extract_pages(path: Path) -> list[tuple[int | None, str]]:
    if path.suffix.lower() != ".pdf":
        return [(None, path.read_text(encoding="utf-8").strip())]

    from pypdf import PdfReader

    return [
        (
            page_number,
            "".join(
                char if char in "\n\t\r" or ord(char) >= 32 else " "
                for char in (page.extract_text() or "")
            ),
        )
        for page_number, page in enumerate(PdfReader(path).pages, start=1)
    ]


def ingest_document(file_path: str, source_name: str | None = None) -> int:
    api_key = os.getenv("PINECONE_API_KEY")
    index_name = os.getenv("PINECONE_INDEX_NAME")
    index_host = os.getenv("PINECONE_INDEX_HOST")
    if not api_key or not (index_host or index_name):
        raise RuntimeError(
            "Set PINECONE_API_KEY and PINECONE_INDEX_HOST "
            "(or PINECONE_INDEX_NAME) in .env"
        )

    path = Path(file_path)
    pages = _extract_pages(path)
    page_chunks = [
        (page_number, chunk)
        for page_number, text in pages
        for chunk in _chunks(text)
    ]
    if not page_chunks:
        raise ValueError(f"No text to ingest from {path}")

    from pinecone import Pinecone

    client = Pinecone(api_key=api_key)
    index = client.Index(host=index_host) if index_host else client.Index(index_name)
    source = source_name or path.name
    records = [
        {
            "_id": hashlib.sha256(f"{path.resolve()}:{page}:{i}:{chunk}".encode()).hexdigest(),
            "chunk_text": chunk,
            "source": source,
            **({"page": page} if page is not None else {}),
        }
        for i, (page, chunk) in enumerate(page_chunks)
    ]
    result = index.documents.upsert(namespace="default", documents=records)
    print(f"Ingested {len(records)} chunks from {source}.")
    return result.upserted_count


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Ingest a text or PDF file into Pinecone.")
    parser.add_argument("file", help="Path to a UTF-8 text or PDF file")
    parser.add_argument("--source", help="Source label shown in chat citations")
    args = parser.parse_args()
    ingest_document(args.file, args.source)
