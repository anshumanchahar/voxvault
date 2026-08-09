import logging
from typing import List, Optional
from datetime import datetime, timezone
from uuid import UUID

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue,
    FilterSelector, PointIdsList
)
from qdrant_client.http.exceptions import UnexpectedResponse

from app.models.schemas import MeetingSegment, SessionSummary, OpenAction
from app.config import settings

logger = logging.getLogger(__name__)

COLLECTION_NAME = "meeting_memory"
SESSIONS_COLLECTION = "sessions"
VECTOR_SIZE = 384
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class QdrantService:
    def __init__(self):
        self.client = QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key if settings.qdrant_api_key else None
        )
        self._embedder = None
        self._available = False
        self._ensure_collections()

    def _get_embedder(self):
        if self._embedder is None:
            try:
                from fastembed import TextEmbedding
                self._embedder = TextEmbedding(model_name=EMBEDDING_MODEL)
                logger.info(f"Loaded embedding model: {EMBEDDING_MODEL}")
            except Exception as e:
                logger.error(f"Failed to load fastembed: {e}. Falling back to hash embeddings.")
                self._embedder = None
        return self._embedder

    def _ensure_collections(self):
        for name, size in ((COLLECTION_NAME, VECTOR_SIZE), (SESSIONS_COLLECTION, 1)):
            try:
                self.client.get_collection(name)
                self._available = True
            except UnexpectedResponse as e:
                if "not found" in str(e).lower():
                    try:
                        self.client.create_collection(
                            collection_name=name,
                            vectors_config=VectorParams(size=size, distance=Distance.COSINE)
                        )
                        self._available = True
                        logger.info(f"Created collection: {name}")
                    except Exception as e2:
                        logger.error(f"Failed to create collection {name}: {e2}")
                        self._available = False
                else:
                    logger.error(f"Qdrant collection check failed: {e}")
                    self._available = False
            except Exception as e:
                logger.error(f"Qdrant not reachable ({settings.qdrant_url}): {e}. Memory features disabled.")
                self._available = False

    def _get_embedding(self, text: str) -> List[float]:
        embedder = self._get_embedder()
        if embedder is not None:
            try:
                return list(embedder.embed([text]))[0].astype(float).tolist()
            except Exception as e:
                logger.error(f"Embedding failed: {e}. Falling back to hash embedding.")
        return self._hash_embedding(text)

    def _hash_embedding(self, text: str) -> List[float]:
        import hashlib
        import random
        seed = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        return [rng.uniform(-1, 1) for _ in range(VECTOR_SIZE)]

    @staticmethod
    def _user_filter(user_id: str, session_id: Optional[str] = None, memory_type: Optional[str] = None,
                     status: Optional[str] = None) -> Filter:
        must = [FieldCondition(key="user_id", match=MatchValue(value=user_id))]
        if session_id:
            must.append(FieldCondition(key="session_id", match=MatchValue(value=session_id)))
        if memory_type:
            must.append(FieldCondition(key="memory_type", match=MatchValue(value=memory_type)))
        if status:
            must.append(FieldCondition(key="status", match=MatchValue(value=status)))
        return Filter(must=must)

    def store_segment(self, segment: MeetingSegment, user_id: str, session_id: Optional[str], title: Optional[str] = None) -> bool:
        if not self._available:
            return False
        try:
            if segment.embedding is None:
                segment.embedding = self._get_embedding(segment.text)

            point = PointStruct(
                id=str(segment.id),
                vector=segment.embedding,
                payload={
                    "text": segment.text,
                    "speaker": segment.speaker,
                    "timestamp": segment.timestamp.isoformat(),
                    "user_id": user_id,
                    "session_id": session_id,
                    "title": title,
                    "memory_type": segment.memory_type,
                    "status": segment.status,
                    "audio_url": segment.audio_url,
                }
            )
            self.client.upsert(collection_name=COLLECTION_NAME, points=[point])
            return True
        except Exception as e:
            logger.error(f"Failed to store segment: {e}")
            return False

    def _segment_from_payload(self, point) -> MeetingSegment:
        payload = point.payload
        return MeetingSegment(
            id=UUID(point.id),
            text=payload["text"],
            speaker=payload["speaker"],
            timestamp=payload["timestamp"],
            embedding=None,
            session_id=payload.get("session_id"),
            user_id=payload.get("user_id"),
            title=payload.get("title"),
            memory_type=payload.get("memory_type", "transcript"),
            status=payload.get("status"),
            audio_url=payload.get("audio_url"),
        )

    def search_similar(self, query: str, top_k: int = 5, user_id: Optional[str] = None,
                       session_id: Optional[str] = None, memory_type: Optional[str] = None) -> List[MeetingSegment]:
        if not self._available:
            return []
        try:
            query_embedding = self._get_embedding(query)
            filt = self._user_filter(user_id, session_id, memory_type) if user_id else None
            results = self.client.search(
                collection_name=COLLECTION_NAME,
                query_vector=query_embedding,
                limit=top_k,
                query_filter=filt,
            )
            return [self._segment_from_payload(hit) for hit in results]
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return []

    def clear_all(self, user_id: Optional[str] = None, session_id: Optional[str] = None) -> bool:
        if not self._available:
            return False
        try:
            if user_id:
                self.client.delete(
                    collection_name=COLLECTION_NAME,
                    points_selector=FilterSelector(filter=self._user_filter(user_id, session_id))
                )
            else:
                self.client.delete(
                    collection_name=COLLECTION_NAME,
                    points_selector=FilterSelector(filter=Filter(must=[]))
                )
            return True
        except Exception as e:
            logger.error(f"Clear failed: {e}")
            return False

    def get_all_segments(self, user_id: Optional[str] = None,
                         session_id: Optional[str] = None,
                         memory_type: Optional[str] = None,
                         status: Optional[str] = None) -> List[MeetingSegment]:
        if not self._available:
            return []
        try:
            filt = self._user_filter(user_id, session_id, memory_type, status) if user_id else None
            results = self.client.scroll(
                collection_name=COLLECTION_NAME, limit=10_000, scroll_filter=filt
            )
            return [self._segment_from_payload(point) for point in results[0]]
        except Exception as e:
            logger.error(f"Get all failed: {e}")
            return []

    def get_open_actions(self, user_id: str) -> List[OpenAction]:
        segments = self.get_all_segments(user_id=user_id, memory_type="action", status="open")
        actions = []
        for s in segments:
            actions.append(OpenAction(
                id=s.id,
                text=s.text,
                session_id=s.session_id,
                title=s.title,
                timestamp=s.timestamp,
            ))
        return actions

    def update_segment_status(self, user_id: str, segment_id: str, status: str) -> bool:
        """Flip an action item between open/done by re-embedding the stored text."""
        if not self._available:
            return False
        try:
            match = self.client.retrieve(
                collection_name=COLLECTION_NAME,
                ids=[segment_id],
                with_payload=True,
                with_vectors=False,
            )
            if not match:
                return False
            point = match[0]
            if point.payload.get("user_id") != user_id:
                return False
            payload = dict(point.payload)
            payload["status"] = status
            vector = self._get_embedding(payload["text"])
            self.client.upsert(collection_name=COLLECTION_NAME, points=[PointStruct(
                id=segment_id, vector=vector, payload=payload
            )])
            return True
        except Exception as e:
            logger.error(f"Update segment status failed: {e}")
            return False

    def delete_segment(self, user_id: str, segment_id: str) -> bool:
        """Delete a single memory, but only if it belongs to the user."""
        if not self._available:
            return False
        try:
            match = self.client.retrieve(
                collection_name=COLLECTION_NAME,
                ids=[segment_id],
                with_payload=True,
                with_vectors=False,
            )
            if not match:
                return False
            if match[0].payload.get("user_id") != user_id:
                return False
            self.client.delete(
                collection_name=COLLECTION_NAME,
                points_selector=PointIdsList(points=[segment_id])
            )
            return True
        except Exception as e:
            logger.error(f"Delete segment failed: {e}")
            return False

    # ------------------- sessions -------------------
    def get_sessions(self, user_id: str) -> List[SessionSummary]:
        if not self._available:
            return []
        try:
            results = self.client.scroll(
                collection_name=SESSIONS_COLLECTION,
                limit=10_000,
                scroll_filter=self._user_filter(user_id),
                with_payload=True,
            )
            sessions = []
            for point in results[0]:
                p = point.payload
                sessions.append(SessionSummary(
                    session_id=p.get("session_id", ""),
                    title=p.get("title", "Untitled"),
                    started_at=p.get("started_at", _now_iso()),
                    updated_at=p.get("updated_at", p.get("started_at", _now_iso())),
                    segment_count=int(p.get("segment_count", 0)),
                    summary=p.get("summary"),
                ))
            sessions.sort(key=lambda s: s.updated_at, reverse=True)
            return sessions
        except Exception as e:
            logger.error(f"Get sessions failed: {e}")
            return []

    def get_session(self, user_id: str, session_id: str) -> Optional[SessionSummary]:
        for s in self.get_sessions(user_id):
            if s.session_id == session_id:
                return s
        return None

    def touch_session(self, user_id: str, session_id: str, title: Optional[str] = None,
                      started_at: Optional[str] = None, increment: bool = True) -> bool:
        """Create/refresh a session record. increment=False when creating an empty
        session so segment_count stays accurate."""
        if not self._available:
            return False
        try:
            existing = self.get_session(user_id, session_id)
            payload = {
                "user_id": user_id,
                "session_id": session_id,
                "title": title or (existing.title if existing else "Untitled"),
                "started_at": started_at or (existing.started_at.isoformat() if existing else _now_iso()),
                "updated_at": _now_iso(),
                "segment_count": (existing.segment_count if existing else 0) + (1 if increment else 0),
                "summary": existing.summary if existing else None,
            }
            point = PointStruct(id=session_id, vector=[1.0], payload=payload)
            self.client.upsert(collection_name=SESSIONS_COLLECTION, points=[point])
            return True
        except Exception as e:
            logger.error(f"Touch session failed: {e}")
            return False

    def rename_session(self, user_id: str, session_id: str, title: str) -> bool:
        if not self._available:
            return False
        try:
            existing = self.get_session(user_id, session_id)
            if not existing:
                return False
            payload = {
                "user_id": user_id,
                "session_id": session_id,
                "title": title,
                "started_at": existing.started_at.isoformat(),
                "updated_at": _now_iso(),
                "segment_count": existing.segment_count,
                "summary": existing.summary,
            }
            point = PointStruct(id=session_id, vector=[1.0], payload=payload)
            self.client.upsert(collection_name=SESSIONS_COLLECTION, points=[point])
            return True
        except Exception as e:
            logger.error(f"Rename session failed: {e}")
            return False

    def delete_session(self, user_id: str, session_id: str) -> bool:
        if not self._available:
            return False
        try:
            self.client.delete(
                collection_name=SESSIONS_COLLECTION,
                points_selector=FilterSelector(filter=self._user_filter(user_id, session_id))
            )
            self.clear_all(user_id=user_id, session_id=session_id)
            return True
        except Exception as e:
            logger.error(f"Delete session failed: {e}")
            return False


qdrant_service = QdrantService()