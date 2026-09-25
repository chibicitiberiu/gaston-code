"""AttachmentService: validation, fetching, conversion and multimodal formatting.

Enforces strict path containment on workspace attachments (docs/BACKLOG.md 04.24),
fetches external attachments via ATTGET streaming (SyncTransport), converts
non-native images (BMP, PCX, etc.) to PNG via Pillow, inlines text/code files
under declared client caps, and packages image/text content blocks for Claude SDK turns.
"""

from __future__ import annotations

import base64
import logging
import os
from collections.abc import AsyncIterable
from typing import Any

from ..files.synctransport import SyncTransport
from ..guest import GuestSession
from ..status import SyncStatus

log = logging.getLogger("bridge.attachment")

NATIVE_IMAGES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class AttachmentService:
    """Validates attachment targets and formats attachment blocks for turns."""

    def __init__(
        self,
        connection: Any,
        workspace: Any,
        sync: Any,
    ) -> None:
        self.connection = connection
        self.workspace = workspace
        self.sync = sync

    def validate_attach_target(
        self, guest: GuestSession, attach: str
    ) -> str | None:
        """Validate and resolve attachment relative path within the guest's work directory.

        Enforces strict path containment (docs/BACKLOG.md 04.24), rejecting path traversal,
        absolute paths, drive letters, and symlinks resolving outside the work directory.
        Returns the absolute local path if valid and contained, or None if invalid.
        """
        if not attach or not isinstance(attach, str) or "\0" in attach or ":" in attach:
            return None
        clean = attach.replace("\\", "/").strip()
        if not clean or clean.startswith("/") or os.path.isabs(clean):
            return None
        work_dir = (
            self.workspace.work_dir(guest)
            if hasattr(self.workspace, "work_dir")
            else (getattr(guest, "work_dir", "") or os.getcwd())
        )
        base = os.path.abspath(work_dir)
        target = os.path.abspath(os.path.join(base, clean))
        try:
            if os.path.commonpath([base, target]) != base:
                return None
            base_real = os.path.realpath(base)
            target_real = os.path.realpath(target)
            if os.path.commonpath([base_real, target_real]) != base_real:
                return None
        except ValueError:
            return None
        if target == base:
            return None
        return target

    def parse_display_names(
        self, attach: str | None = None, attach_name: str | None = None
    ) -> list[str]:
        """Extract clean display names from attach and attach_name headers."""
        display_names: list[str] = []
        if attach_name:
            display_names.extend(
                [n.strip() for n in attach_name.split("|") if n.strip()]
            )
        if attach:
            display_names.extend(
                [
                    os.path.basename(p.replace("\\", "/").strip()) or p.strip()
                    for p in attach.split("|")
                    if p.strip()
                ]
            )
        return display_names

    def format_display_text(self, text: str, display_names: list[str]) -> str:
        """Add [Attached: ...] banner to transcript text if attachments present."""
        if display_names and "[Attached:" not in text:
            return f"[Attached: {', '.join(display_names)}]\n{text}"
        return text

    async def assemble_prompt(
        self,
        guest: GuestSession,
        text: str,
        *,
        attach: str | None = None,
        attach_id: str | None = None,
        attach_name: str | None = None,
    ) -> str | AsyncIterable[dict]:
        """Assemble multimodal Claude content blocks or formatted prompt text."""
        if not (attach or attach_id):
            return text

        items: list[dict] = []
        if attach_id and attach_name:
            ids = [i.strip() for i in attach_id.split("|") if i.strip()]
            names = [n.strip() for n in attach_name.split("|") if n.strip()]
            for aid, aname in zip(ids, names):
                items.append(
                    {
                        "kind": "external",
                        "id": aid,
                        "name": aname,
                        "path": None,
                    }
                )
        if attach:
            for p in attach.split("|"):
                p = p.strip()
                if p:
                    clean_path = p.replace("\\", "/").strip()
                    fn = os.path.basename(clean_path) or clean_path
                    items.append(
                        {
                            "kind": "workspace",
                            "id": None,
                            "name": clean_path,
                            "display_name": fn,
                            "path": clean_path,
                        }
                    )

        image_blocks: list[dict] = []
        text_blocks: list[str] = []

        for item in items:
            filename = item["name"]
            ext = os.path.splitext(filename)[1].lower()
            att_bytes: bytes | None = None
            file_too_large_image = False
            if item["kind"] == "external" and item["id"]:
                try:
                    transport = SyncTransport(guest)
                    res = await transport.get_attachment(item["id"])
                    if res.status == SyncStatus.OK:
                        att_bytes = res.data
                    else:
                        log.warning(
                            "failed to fetch attachment %s: %s",
                            item["id"],
                            res.status,
                        )
                except Exception as exc:
                    log.warning(
                        "exception fetching attachment %s: %s",
                        item["id"],
                        exc,
                    )
            elif item["kind"] == "workspace" and item["path"]:
                clean_attach = item["path"]
                target = self.validate_attach_target(guest, clean_attach)
                has_sync = bool(
                    getattr(self.sync, "config", None)
                    and getattr(self.sync.config, "sync_dir", None)
                ) or bool(getattr(self.connection, "sync_dir", None))
                if target and not os.path.exists(target) and has_sync:
                    try:
                        run_sync = getattr(self.sync, 'run_sync', None)
                        if run_sync is not None:
                            await run_sync(
                                "attachment not found locally", guest=guest
                            )
                    except Exception as exc:
                        log.warning("sync before attachment failed: %s", exc)
                if target and os.path.isfile(target):
                    try:
                        file_size = os.path.getsize(target)
                        if ext in NATIVE_IMAGES and file_size > 10 * 1024 * 1024:
                            file_too_large_image = True
                            log.warning(
                                "image attachment %s exceeds 10MB limit (%d bytes)",
                                clean_attach,
                                file_size,
                            )
                        else:
                            with open(target, "rb") as f:
                                att_bytes = f.read()
                    except OSError as exc:
                        log.warning(
                            "failed to read attachment %s: %s", target, exc
                        )

            if file_too_large_image:
                prefix = (
                    "Attached workspace file"
                    if item["kind"] == "workspace"
                    else "Referenced file"
                )
                text_blocks.append(
                    f"[{prefix}: {filename} (file too large for image block)]\n"
                )
                continue

            image_data: bytes | None = None
            media_type: str = "image/png"
            if att_bytes:
                if ext in NATIVE_IMAGES and len(att_bytes) <= 10 * 1024 * 1024:
                    image_data = att_bytes
                    media_type = NATIVE_IMAGES[ext]
                else:
                    try:
                        import io

                        from PIL import Image

                        with Image.open(io.BytesIO(att_bytes)) as img:
                            buf = io.BytesIO()
                            if img.mode not in ("RGB", "RGBA"):
                                img = img.convert(
                                    "RGBA"
                                    if (
                                        "transparency" in img.info
                                        or img.mode == "P"
                                    )
                                    else "RGB"
                                )
                            img.save(buf, format="PNG")
                            converted = buf.getvalue()
                            if len(converted) <= 10 * 1024 * 1024:
                                image_data = converted
                                media_type = "image/png"
                            else:
                                log.warning(
                                    "converted image %s exceeds 10MB limit (%d bytes)",
                                    filename,
                                    len(converted),
                                )
                    except Exception:
                        pass

            if image_data:
                b64_data = base64.b64encode(image_data).decode("ascii")
                image_blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64_data,
                        },
                    }
                )
            else:
                label = (
                    "Attached workspace file"
                    if item["kind"] == "workspace"
                    else "Attached file"
                )
                ref_label = (
                    "Attached workspace file"
                    if item["kind"] == "workspace"
                    else "Referenced file"
                )
                if att_bytes is not None:
                    file_size = len(att_bytes)
                    if file_size == 0:
                        text_blocks.append(
                            f"[{label}: {filename} (empty file)]\n"
                        )
                    elif file_size > 256 * 1024:
                        text_blocks.append(
                            f"[{label}: {filename} "
                            f"({file_size} bytes, file too large to inline directly)]\n"
                        )
                    else:
                        is_binary = b"\x00" in att_bytes
                        content_str: str | None = None
                        if not is_binary:
                            if att_bytes.startswith(b"\xef\xbb\xbf"):
                                try:
                                    content_str = att_bytes[3:].decode("utf-8")
                                except UnicodeDecodeError:
                                    pass
                            if content_str is None:
                                try:
                                    content_str = att_bytes.decode("utf-8")
                                except UnicodeDecodeError:
                                    pass
                            if (
                                content_str is None
                                and hasattr(guest, "caps")
                                and getattr(guest.caps, "acp", None)
                            ):
                                try:
                                    content_str = att_bytes.decode(
                                        guest.caps.acp
                                    )
                                except (UnicodeDecodeError, LookupError):
                                    pass
                            if content_str is None:
                                try:
                                    content_str = att_bytes.decode("cp1252")
                                except UnicodeDecodeError:
                                    pass
                        if content_str is not None:
                            text_blocks.append(
                                f"[{label}: {filename}]\n"
                                f"Contents of {filename}:\n"
                                f"```\n"
                                f"{content_str}\n"
                                f"```\n"
                            )
                        else:
                            text_blocks.append(
                                f"[{ref_label}: {filename} ({file_size} bytes, binary file)]\n"
                            )
                else:
                    text_blocks.append(
                        f"[{label}: {filename} (failed to read file)]\n"
                    )

        display_names = self.parse_display_names(attach, attach_name)
        clean_text = text
        for item in items:
            tag = f"[Attached: {item['name']}]"
            clean_text = (
                clean_text.replace(tag + "\r\n", "")
                .replace(tag + "\n", "")
                .replace(tag, "")
            )
        if display_names:
            tag = f"[Attached: {', '.join(display_names)}]"
            clean_text = (
                clean_text.replace(tag + "\r\n", "")
                .replace(tag + "\n", "")
                .replace(tag, "")
            )
        clean_text = clean_text.strip()
        prompt_text = clean_text if clean_text else text

        if text_blocks:
            header = "".join(text_blocks)
            prompt_text = (
                f"{header}{prompt_text}" if prompt_text else header.strip()
            )

        if image_blocks:

            async def _multimodal_prompt(
                blocks=list(image_blocks), ptext=prompt_text
            ):
                content: list[dict] = list(blocks)
                if ptext:
                    content.append({"type": "text", "text": ptext})
                yield {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": content,
                    },
                    "parent_tool_use_id": None,
                }

            return _multimodal_prompt()
        return prompt_text
