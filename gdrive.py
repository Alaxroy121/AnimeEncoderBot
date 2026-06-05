"""
Google Drive upload module for AnimeEncoderBot.
Uses a Service Account (SA) JSON key to upload files > 2GB
and generate shareable links.
"""

import logging
import mimetypes
from pathlib import Path
from typing import Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaUploadProgress

from config import Config

logger = logging.getLogger(__name__)

# Google Drive API scopes
SCOPES = ["https://www.googleapis.com/auth/drive"]


class GDriveUploader:
    """Upload files to Google Drive using a Service Account."""

    def __init__(self) -> None:
        self._service = None
        self._available: Optional[bool] = None

    def is_configured(self) -> bool:
        """Check if GDrive credentials are configured."""
        if self._available is not None:
            return self._available

        sa_path = Path(Config.GDRIVE_SA_JSON)
        folder_id = Config.GDRIVE_FOLDER_ID

        if not sa_path.exists():
            logger.info("GDrive SA JSON not found at %s — GDrive upload disabled", sa_path)
            self._available = False
            return False

        if not folder_id or folder_id == "your_folder_id_here":
            logger.info("GDRIVE_FOLDER_ID not set — GDrive upload disabled")
            self._available = False
            return False

        self._available = True
        return True

    def _get_service(self):
        """Build and cache the Google Drive API service."""
        if self._service is not None:
            return self._service

        try:
            creds = Credentials.from_service_account_file(
                Config.GDRIVE_SA_JSON, scopes=SCOPES
            )
            self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
            logger.info("Google Drive API service initialized")
            return self._service
        except Exception as e:
            logger.error("Failed to initialize GDrive service: %s", e)
            raise RuntimeError(f"GDrive auth failed: {e}")

    async def upload(
        self,
        file_path: str,
        filename: Optional[str] = None,
        progress_callback=None,
    ) -> dict:
        """Upload a file to Google Drive.

        Args:
            file_path: Local path to the file.
            filename: Override filename (defaults to basename of file_path).
            progress_callback: Called with (bytes_uploaded, total_bytes).

        Returns:
            dict with keys: file_id, file_name, file_size, link
        """
        import asyncio

        if not self.is_configured():
            raise RuntimeError("Google Drive is not configured")

        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        name = filename or path.name
        file_size = path.stat().st_size
        mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"

        logger.info("Uploading to GDrive: %s (%d bytes)", name, file_size)

        # Run the blocking upload in a thread executor
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            self._upload_sync,
            file_path, name, mime_type, file_size, progress_callback,
        )
        return result

    def _upload_sync(
        self,
        file_path: str,
        filename: str,
        mime_type: str,
        file_size: int,
        progress_callback=None,
    ) -> dict:
        """Synchronous upload (runs in executor)."""
        service = self._get_service()

        file_metadata = {
            "name": filename,
            "parents": [Config.GDRIVE_FOLDER_ID],
        }

        media = MediaFileUpload(
            file_path,
            mimetype=mime_type,
            resumable=True,
            chunksize=50 * 1024 * 1024,  # 50MB chunks
        )

        request = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, name, size, webViewLink, webContentLink",
            supportsAllDrives=True,
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status and progress_callback:
                try:
                    progress_callback(
                        int(status.resumable_progress),
                        file_size,
                    )
                except Exception:
                    pass

        file_id = response.get("id")

        # Make file publicly accessible (anyone with the link)
        try:
            service.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": "reader"},
                supportsAllDrives=True,
            ).execute()
        except Exception as e:
            logger.warning("Could not set public permission: %s", e)

        link = response.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"

        result = {
            "file_id": file_id,
            "file_name": response.get("name", filename),
            "file_size": file_size,
            "link": link,
        }

        logger.info("GDrive upload complete: %s → %s", filename, link)
        return result


# Global instance
gdrive = GDriveUploader()
