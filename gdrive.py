"""
Google Drive upload module for AnimeEncoderBot.
Uses OAuth2 refresh token to upload files and generate shareable links.
"""

import logging
import mimetypes
from pathlib import Path
from typing import Optional, Callable

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from config import Config

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]


class GDriveUploader:
    """Upload files to Google Drive using OAuth2."""

    def __init__(self) -> None:
        self._service = None
        self._available: Optional[bool] = None

    def is_configured(self) -> bool:
        """Check if GDrive credentials are configured."""
        if self._available is not None:
            return self._available

        folder_id = Config.GDRIVE_FOLDER_ID
        if not folder_id or folder_id == "your_folder_id_here":
            logger.info("GDRIVE_FOLDER_ID not set — GDrive upload disabled")
            self._available = False
            return False

        # OAuth2 mode
        if Config.GDRIVE_CLIENT_ID and Config.GDRIVE_CLIENT_SECRET and Config.GDRIVE_REFRESH_TOKEN:
            self._available = True
            return True

        # Legacy SA mode (deprecated — Google removed SA storage quota)
        sa_path = Path(Config.GDRIVE_SA_JSON)
        if sa_path.exists():
            logger.warning("Service Account auth is deprecated — use OAuth2 instead")
            self._available = True
            return True

        logger.info("GDrive credentials not configured — upload disabled")
        self._available = False
        return False

    def _get_service(self):
        """Build and cache the Drive API service."""
        if self._service is not None:
            return self._service

        # OAuth2 mode (preferred)
        if Config.GDRIVE_CLIENT_ID and Config.GDRIVE_CLIENT_SECRET and Config.GDRIVE_REFRESH_TOKEN:
            creds = Credentials(
                token=None,
                refresh_token=Config.GDRIVE_REFRESH_TOKEN,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=Config.GDRIVE_CLIENT_ID,
                client_secret=Config.GDRIVE_CLIENT_SECRET,
                scopes=SCOPES,
            )
            logger.info("GDrive: using OAuth2 credentials")
        else:
            # Legacy SA fallback
            from google.oauth2.service_account import Credentials as SACredentials
            sa_path = Path(Config.GDRIVE_SA_JSON)
            creds = SACredentials.from_service_account_file(str(sa_path), scopes=SCOPES)
            logger.info("GDrive: using Service Account credentials (deprecated)")

        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    async def upload(
        self,
        file_path: str,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
    ) -> dict:
        """Upload a file to Google Drive and return file info with shareable link."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        if filename is None:
            filename = path.name

        if mime_type is None:
            mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"

        file_size = path.stat().st_size
        logger.info("Uploading to GDrive: %s (%d bytes, %s)", filename, file_size, mime_type)

        service = self._get_service()

        file_metadata = {"name": filename}
        if Config.GDRIVE_FOLDER_ID:
            file_metadata["parents"] = [Config.GDRIVE_FOLDER_ID]

        media = MediaFileUpload(
            str(path),
            mimetype=mime_type,
            resumable=True,
            chunksize=50 * 1024 * 1024,  # 50MB chunks
        )

        request = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, name, size, webViewLink, webContentLink",
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
