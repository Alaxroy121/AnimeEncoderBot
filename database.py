"""
MongoDB database operations for AnimeEncoderBot.
Handles users, tasks, and stats collections.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from config import Config

logger = logging.getLogger(__name__)


class Database:
    """Async MongoDB wrapper for bot data."""

    def __init__(self) -> None:
        self._client: Optional[AsyncIOMotorClient] = None
        self._db: Optional[AsyncIOMotorDatabase] = None

    async def connect(self) -> None:
        """Establish MongoDB connection and create indexes."""
        self._client = AsyncIOMotorClient(Config.MONGO_URI)
        self._db = self._client.get_default_database()
        logger.info("Connected to MongoDB: %s", self._db.name)

        # Create indexes
        await self._db.users.create_index("user_id", unique=True)
        await self._db.tasks.create_index("task_id", unique=True)
        await self._db.tasks.create_index("user_id")
        await self._db.tasks.create_index("status")
        await self._db.tasks.create_index([("status", 1), ("priority", -1), ("created_at", 1)])

    async def close(self) -> None:
        """Close the database connection."""
        if self._client:
            self._client.close()
            logger.info("MongoDB connection closed")

    # ── User Operations ──────────────────────────────────────────────

    async def add_user(self, user_id: int, username: str = "") -> dict:
        """Add or update a user. Returns the user document."""
        now = datetime.now(timezone.utc)
        result = await self._db.users.find_one_and_update(
            {"user_id": user_id},
            {
                "$set": {"username": username, "last_seen": now},
                "$setOnInsert": {
                    "user_id": user_id,
                    "join_date": now,
                    "is_banned": False,
                    "total_tasks": 0,
                    "settings": {
                        "default_codec": Config.DEFAULT_CODEC,
                        "default_quality": "medium",
                        "default_preset": "medium",
                        "notifications": True,
                    },
                },
            },
            upsert=True,
            return_document=True,
        )
        return result

    async def get_user(self, user_id: int) -> Optional[dict]:
        """Get a user by ID."""
        return await self._db.users.find_one({"user_id": user_id})

    async def is_banned(self, user_id: int) -> bool:
        """Check if a user is banned."""
        user = await self._db.users.find_one({"user_id": user_id}, {"is_banned": 1})
        return user.get("is_banned", False) if user else False

    async def ban_user(self, user_id: int) -> bool:
        """Ban a user. Returns True if user existed."""
        result = await self._db.users.update_one(
            {"user_id": user_id}, {"$set": {"is_banned": True}}
        )
        return result.modified_count > 0

    async def unban_user(self, user_id: int) -> bool:
        """Unban a user. Returns True if user existed."""
        result = await self._db.users.update_one(
            {"user_id": user_id}, {"$set": {"is_banned": False}}
        )
        return result.modified_count > 0

    async def get_all_user_ids(self) -> list[int]:
        """Get all non-banned user IDs (for broadcast)."""
        cursor = self._db.users.find(
            {"is_banned": {"$ne": True}}, {"user_id": 1}
        )
        return [doc["user_id"] async for doc in cursor]

    async def get_user_count(self) -> int:
        """Get total user count."""
        return await self._db.users.count_documents({})

    async def update_user_settings(self, user_id: int, settings: dict) -> None:
        """Update user settings."""
        update_fields = {f"settings.{k}": v for k, v in settings.items()}
        await self._db.users.update_one(
            {"user_id": user_id}, {"$set": update_fields}
        )

    async def increment_user_tasks(self, user_id: int) -> None:
        """Increment a user's total task count."""
        await self._db.users.update_one(
            {"user_id": user_id}, {"$inc": {"total_tasks": 1}}
        )

    # ── Task Operations ──────────────────────────────────────────────

    async def create_task(self, task_data: dict) -> str:
        """Create a new task. Returns the task_id."""
        now = datetime.now(timezone.utc)
        task_data.update({
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
            "progress": 0.0,
            "error": None,
        })
        task_data.setdefault("status", "queued")
        task_data.setdefault("priority", 0)
        result = await self._db.tasks.insert_one(task_data)
        logger.info("Created task %s for user %s", task_data["task_id"], task_data["user_id"])
        return task_data["task_id"]

    async def get_task(self, task_id: str) -> Optional[dict]:
        """Get a task by ID."""
        return await self._db.tasks.find_one({"task_id": task_id})

    async def get_user_active_task(self, user_id: int) -> Optional[dict]:
        """Get the user's current active (queued/processing) task."""
        return await self._db.tasks.find_one({
            "user_id": user_id,
            "status": {"$in": ["queued", "processing"]},
        })

    async def update_task(self, task_id: str, updates: dict) -> None:
        """Update task fields."""
        updates["updated_at"] = datetime.now(timezone.utc)
        await self._db.tasks.update_one(
            {"task_id": task_id}, {"$set": updates}
        )

    async def get_queued_tasks(self, limit: int = 50) -> list[dict]:
        """Get queued tasks ordered by priority (desc) then created_at (asc)."""
        cursor = self._db.tasks.find(
            {"status": "queued"}
        ).sort([("priority", -1), ("created_at", 1)]).limit(limit)
        return await cursor.to_list(length=limit)

    async def get_processing_count(self) -> int:
        """Get number of currently processing tasks."""
        return await self._db.tasks.count_documents({"status": "processing"})

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a task. Returns True if it was queued/processing."""
        result = await self._db.tasks.update_one(
            {"task_id": task_id, "status": {"$in": ["queued", "processing"]}},
            {"$set": {"status": "cancelled", "updated_at": datetime.now(timezone.utc)}},
        )
        return result.modified_count > 0

    async def get_queue_position(self, task_id: str) -> int:
        """Get a task's position in the queue (1-based). Returns 0 if not queued."""
        task = await self.get_task(task_id)
        if not task or task["status"] != "queued":
            return 0
        count = await self._db.tasks.count_documents({
            "status": "queued",
            "$or": [
                {"priority": {"$gt": task.get("priority", 0)}},
                {
                    "priority": task.get("priority", 0),
                    "created_at": {"$lt": task["created_at"]},
                },
            ],
        })
        return count + 1

    async def get_user_tasks(self, user_id: int, limit: int = 10) -> list[dict]:
        """Get a user's recent tasks."""
        cursor = self._db.tasks.find(
            {"user_id": user_id}
        ).sort("created_at", -1).limit(limit)
        return await cursor.to_list(length=limit)

    # ── Stats Operations ─────────────────────────────────────────────

    async def increment_stats(self, task_type: str, data_bytes: int = 0) -> None:
        """Increment global statistics."""
        field = "total_encodes" if task_type == "encode" else "total_upscales"
        await self._db.stats.update_one(
            {"_id": "global"},
            {
                "$inc": {field: 1, "total_data_processed": data_bytes},
                "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
            },
            upsert=True,
        )

    async def get_stats(self) -> dict:
        """Get global statistics."""
        stats = await self._db.stats.find_one({"_id": "global"})
        if not stats:
            return {"total_encodes": 0, "total_upscales": 0, "total_data_processed": 0}
        return stats

    async def get_recent_tasks(self, limit: int = 20) -> list[dict]:
        """Get recent tasks across all users."""
        cursor = self._db.tasks.find().sort("created_at", -1).limit(limit)
        return await cursor.to_list(length=limit)


# Global database instance
db = Database()
