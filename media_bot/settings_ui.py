from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, PhotoSize, Update
from telegram.ext import ContextTypes

from .storage import JobRecord, Preset

LOGGER = logging.getLogger(__name__)

_PAGE_SIZE = 6


class _State:
    MENU = "settings_menu"
    PRESET_LIST = "preset_list"
    PRESET_CREATE_NAME = "preset_create_name"
    PRESET_CREATE_CAPTION = "preset_create_caption"
    PRESET_CREATE_CAPTION_COLOR = "preset_create_caption_color"
    PRESET_CREATE_CAPTION_STYLE = "preset_create_caption_style"
    PRESET_CREATE_CAPTION_POS = "preset_create_caption_pos"
    PRESET_CREATE_VOICE = "preset_create_voice"
    PRESET_CREATE_VOICE_TEXT = "preset_create_voice_text"
    PRESET_CREATE_VOICE_QUALITY = "preset_create_voice_quality"
    PRESET_CREATE_VOICE_SPEED = "preset_create_voice_speed"
    PRESET_CREATE_TTS_ENGINE = "preset_create_tts_engine"
    PRESET_CREATE_BANNER = "preset_create_banner"
    PRESET_CREATE_BANNER_POS = "preset_create_banner_pos"
    PRESET_CREATE_BANNER_SCALE = "preset_create_banner_scale"
    PRESET_CREATE_BANNER_UPLOAD = "preset_create_banner_upload"
    PRESET_EDIT = "preset_edit"
    PRESET_EDIT_FIELD = "preset_edit_field"
    STATS = "stats"
    EDIT_SOURCE = "edit_source"
    EDIT_PRESET_SELECT = "edit_preset_select"
    EDIT_PROCESSING = "edit_processing"


@dataclass
class FlowState:
    action: str
    page: int = 0
    data: dict[str, Any] = field(default_factory=dict)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["settings_flow"] = FlowState(action=_State.MENU)
    await _show_menu(update, context)


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()

    flow: FlowState = context.user_data.get("settings_flow")
    if flow is None:
        flow = FlowState(action=_State.MENU)
        context.user_data["settings_flow"] = flow

    if query.data == "settings:menu":
        flow.action = _State.MENU
        flow.data.clear()
        await _show_menu(update, context)
        return

    if query.data.startswith("settings:presets"):
        flow.action = _State.PRESET_LIST
        flow.page = 0
        await _show_preset_list(update, context)
        return

    if query.data == "settings:create_preset":
        flow.action = _State.PRESET_CREATE_NAME
        flow.data.clear()
        await _edit_message(query, "Send the preset name (e.g. \"default\"):")
        return

    if query.data == "settings:profile_banner":
        flow.action = "set_profile_banner"
        await _edit_message(query, "Send a photo to use as your profile banner:")
        return

    if query.data == "settings:stats":
        flow.action = _State.STATS
        await _show_stats(update, context)
        return

    if query.data == "settings:edit_source":
        flow.action = _State.EDIT_SOURCE
        flow.page = 0
        await _show_edit_source(update, context)
        return

    if query.data.startswith("preset:page:"):
        flow.page = int(query.data.split(":")[-1])
        await _show_preset_list(update, context)
        return

    if query.data.startswith("preset:edit:"):
        preset_id = int(query.data.split(":")[-1])
        flow.action = _State.PRESET_EDIT
        flow.data["preset_id"] = preset_id
        await _show_preset_edit(update, context, preset_id)
        return

    if query.data.startswith("preset:delete:"):
        preset_id = int(query.data.split(":")[-1])
        await _delete_preset_and_refresh(update, context, preset_id)
        return

    if query.data.startswith("preset:share:"):
        preset_id = int(query.data.split(":")[-1])
        await _share_preset(update, context, preset_id)
        return

    if query.data.startswith("preset:field:"):
        parts = query.data.split(":")
        preset_id = int(parts[2])
        field_name = parts[3]
        await _start_edit_field(update, context, preset_id, field_name)
        return

    if query.data == "preset:back":
        flow.action = _State.PRESET_LIST
        flow.page = 0
        await _show_preset_list(update, context)
        return

    if query.data.startswith("edit:source:"):
        job_id = int(query.data.split(":")[-1])
        flow.action = _State.EDIT_PRESET_SELECT
        flow.data["source_job_id"] = job_id
        await _show_edit_preset_select(update, context, job_id)
        return

    if query.data.startswith("edit:page:"):
        flow.page = int(query.data.split(":")[-1])
        await _show_edit_source(update, context)
        return

    if query.data.startswith("edit:preset:"):
        preset_id = int(query.data.split(":")[-1])
        await _start_edit_process(update, context, preset_id)
        return

    if query.data == "edit:use_temp":
        await _start_temp_edit(update, context)
        return

    if query.data == "edit:back":
        flow.action = _State.EDIT_SOURCE
        flow.page = 0
        await _show_edit_source(update, context)
        return

    if query.data.startswith("edit:pool:"):
        edit_id = int(query.data.split(":")[-1])
        await _add_edit_to_pool(update, context, edit_id)
        return

    if query.data.startswith("edit:config:"):
        edit_id = int(query.data.split(":")[-1])
        await _start_editconfig_from_settings(update, context, edit_id)
        return


async def settings_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    flow: FlowState = context.user_data.get("settings_flow")
    if flow is None or not update.message or not update.message.photo:
        return False
    if flow.action not in (_State.PRESET_CREATE_BANNER, "set_profile_banner"):
        return False
    user = update.effective_user
    if user is None:
        return False
    storage_dir: Path = context.application.bot_data.get("storage_dir", Path("runtime/jobs"))
    banners_dir = storage_dir / "banners"
    banners_dir.mkdir(parents=True, exist_ok=True)

    photo: PhotoSize = update.message.photo[-1]
    file = await photo.get_file()
    dest = banners_dir / f"banner_{user.id}.png"
    await file.download_to_drive(dest)

    if flow.action == "set_profile_banner":
        flow.action = _State.MENU
        await update.message.reply_text("Profile banner saved!")
        await _show_menu(update, context)
        return True

    if flow.action == _State.PRESET_CREATE_BANNER:
        flow.data["banner_path"] = str(dest)
        flow.action = _State.PRESET_CREATE_BANNER_POS
        await update.message.reply_text("Banner position? (top, bottom, overlay):")
        return True

    return False


async def settings_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    flow: FlowState = context.user_data.get("settings_flow")
    if flow is None or not update.message or not update.message.text:
        return False

    text = update.message.text.strip()
    db_path: Path = context.application.bot_data["db_path"]

    if flow.action == _State.PRESET_CREATE_NAME:
        flow.data["name"] = text
        flow.data["auto_captions"] = True
        flow.action = _State.PRESET_CREATE_CAPTION_COLOR
        await update.message.reply_text("Caption border color? (white, black, yellow, red, blue, green):")
        return True

    if flow.action == _State.PRESET_CREATE_CAPTION_COLOR:
        cap = text.lower()
        if cap not in {"white", "black", "yellow", "red", "blue", "green"}:
            await update.message.reply_text("Choose: white, black, yellow, red, blue, green")
            return True
        flow.data["caption_color"] = cap
        flow.action = _State.PRESET_CREATE_CAPTION_STYLE
        await update.message.reply_text("Caption style? (basic, bold, bubble):")
        return True

    if flow.action == _State.PRESET_CREATE_CAPTION_STYLE:
        style = text.lower()
        if style not in {"basic", "bold", "bubble"}:
            await update.message.reply_text("Choose: basic, bold, bubble")
            return True
        flow.data["caption_style"] = style
        flow.action = _State.PRESET_CREATE_CAPTION_POS
        await update.message.reply_text("Caption position? (low, middle, high):")
        return True

    if flow.action == _State.PRESET_CREATE_CAPTION_POS:
        pos = text.lower()
        if pos not in {"low", "middle", "high"}:
            await update.message.reply_text("Choose: low, middle, high")
            return True
        flow.data["caption_position"] = pos
        flow.action = _State.PRESET_CREATE_VOICE
        await update.message.reply_text("Voice-over voice name (or /skip for none):")
        return True

    if flow.action == _State.PRESET_CREATE_VOICE:
        if text.lower() != "/skip":
            flow.data["voice_over_voice"] = text
        flow.action = _State.PRESET_CREATE_VOICE_TEXT
        await update.message.reply_text("Voice-over text to speak (or /skip for none):")
        return True

    if flow.action == _State.PRESET_CREATE_VOICE_TEXT:
        if text.lower() != "/skip":
            flow.data["voice_text"] = text
        flow.action = _State.PRESET_CREATE_VOICE_QUALITY
        await update.message.reply_text("Voice quality? (basic, premium):")
        return True

    if flow.action == _State.PRESET_CREATE_VOICE_QUALITY:
        quality = text.lower()
        if quality not in {"basic", "premium"}:
            await update.message.reply_text("Choose: basic, premium")
            return True
        flow.data["voice_quality"] = quality
        flow.action = _State.PRESET_CREATE_VOICE_SPEED
        await update.message.reply_text("Voice speed? (0.5 to 2.0, e.g. 1.0):")
        return True

    if flow.action == _State.PRESET_CREATE_VOICE_SPEED:
        try:
            speed = float(text)
            if not (0.5 <= speed <= 2.0):
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number between 0.5 and 2.0")
            return True
        flow.data["voice_speed"] = speed
        flow.action = _State.PRESET_CREATE_TTS_ENGINE
        await update.message.reply_text("TTS engine? (edge-tts, espeak-ng, auto):")
        return True

    if flow.action == _State.PRESET_CREATE_TTS_ENGINE:
        engine = text.lower()
        if engine not in {"edge-tts", "espeak-ng", "auto"}:
            await update.message.reply_text("Choose: edge-tts, espeak-ng, auto")
            return True
        flow.data["tts_engine"] = engine
        flow.action = _State.PRESET_CREATE_BANNER
        await update.message.reply_text("Add a banner/watermark? Send a photo, paste an image URL, or /skip:")
        return True

    if flow.action == _State.PRESET_CREATE_BANNER:
        if text.lower() != "/skip":
            storage_dir: Path = context.application.bot_data.get("storage_dir", Path("runtime/jobs"))
            banner_file = storage_dir / "banners" / f"banner_{user.id if (user := update.effective_user) else 0}.png"
            if text.lower() in ("profile", "p") and banner_file.is_file():
                flow.data["banner_path"] = str(banner_file)
                flow.action = _State.PRESET_CREATE_BANNER_POS
                await update.message.reply_text("Banner position? (top, bottom, overlay):")
            elif text.startswith("http://") or text.startswith("https://"):
                flow.data["banner_url"] = text
                flow.action = _State.PRESET_CREATE_BANNER_POS
                await update.message.reply_text("Banner position? (top, bottom, overlay):")
            else:
                await update.message.reply_text("Send a photo, type 'profile', paste an image URL, or /skip:")
            return True
        flow.action = _State.PRESET_CREATE_BANNER_POS
        await update.message.reply_text("Banner position? (top, bottom, overlay):")
        return True

    if flow.action == _State.PRESET_CREATE_BANNER_POS:
        pos = text.lower()
        if pos not in {"top", "bottom", "overlay"}:
            await update.message.reply_text("Choose: top, bottom, overlay")
            return True
        flow.data["banner_position"] = pos
        flow.action = _State.PRESET_CREATE_BANNER_SCALE
        await update.message.reply_text("Banner scale? (fit, stretch, fill):")
        return True

    if flow.action == _State.PRESET_CREATE_BANNER_SCALE:
        scale = text.lower()
        if scale not in {"fit", "stretch", "fill"}:
            await update.message.reply_text("Choose: fit, stretch, fill")
            return True
        flow.data["banner_scale"] = scale
        await _finalize_preset_create(update, context, db_path)
        return True

    if flow.action == _State.PRESET_EDIT_FIELD:
        preset_id = flow.data.get("preset_id")
        field_name = flow.data.get("field_name")
        if preset_id is None or field_name is None:
            return False
        value = None if text.lower() == "/skip" else text
        valid_opts = {
            "caption_color": {"white", "black", "yellow", "red", "blue", "green"},
            "caption_style": {"basic", "bold", "bubble"},
            "caption_position": {"low", "middle", "high"},
            "voice_quality": {"basic", "premium"},
            "tts_engine": {"edge-tts", "espeak-ng", "auto"},
            "banner_position": {"top", "bottom", "overlay"},
            "banner_scale": {"fit", "stretch", "fill"},
        }
        if field_name == "auto_captions":
            if value is None:
                value = False
            elif value.lower() in ("yes", "y", "on", "true", "1"):
                value = True
            elif value.lower() in ("no", "n", "off", "false", "0"):
                value = False
            else:
                await update.message.reply_text("Enter yes or no")
                return True
        elif field_name in valid_opts:
            opts = valid_opts[field_name]
            if value is not None and value.lower() not in opts:
                await update.message.reply_text(f"Choose: {', '.join(sorted(opts))}")
                return True
            if value is not None:
                value = value.lower()
        if field_name in ("voice_speed",):
            try:
                value = float(text)
                if not (0.5 <= value <= 2.0):
                    raise ValueError
            except ValueError:
                await update.message.reply_text("Enter a number between 0.5 and 2.0")
                return True
        if field_name == "banner_path" and value is not None and not value.startswith("http"):
            await update.message.reply_text("Send a URL for the banner image, or /skip to clear")
            return True
        await _apply_preset_edit(update, context, preset_id, field_name, value)
        return True

    return False


async def _show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("My Presets", callback_data="settings:presets")],
        [InlineKeyboardButton("Create Preset", callback_data="settings:create_preset")],
        [InlineKeyboardButton("Set Profile Banner", callback_data="settings:profile_banner")],
        [InlineKeyboardButton("Stats / History", callback_data="settings:stats")],
        [InlineKeyboardButton("Edit Existing Video", callback_data="settings:edit_source")],
    ])
    msg = "Settings menu:\nChoose an option below."
    if update.message:
        await update.message.reply_text(msg, reply_markup=keyboard)
    elif update.callback_query:
        await _edit_message(update.callback_query, msg, keyboard)


async def _show_preset_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow: FlowState = context.user_data["settings_flow"]
    db_path: Path = context.application.bot_data["db_path"]
    user = update.effective_user
    if user is None:
        return
    presets = await list_presets(db_path, user.id)
    total = len(presets)
    start = flow.page * _PAGE_SIZE
    page = presets[start:start + _PAGE_SIZE]

    rows = []
    for p in page:
        rows.append([InlineKeyboardButton(
            f"{p.name} ({p.caption_style or 'none'})",
            callback_data=f"preset:edit:{p.id}",
        )])
    rows.append([InlineKeyboardButton("Create new", callback_data="settings:create_preset")])

    nav = []
    if flow.page > 0:
        nav.append(InlineKeyboardButton("← Back", callback_data=f"preset:page:{flow.page - 1}"))
    if start + _PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Forward →", callback_data=f"preset:page:{flow.page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("Menu", callback_data="settings:menu")])

    text = f"Your presets ({total}):"
    if total == 0:
        text = "No presets yet. Create one to get started."
    await _edit_or_send(update, text, InlineKeyboardMarkup(rows))


async def _show_preset_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, preset_id: int) -> None:
    db_path: Path = context.application.bot_data["db_path"]
    user = update.effective_user
    if user is None:
        return
    preset = await get_preset_by_share_code(db_path, "") or await _get_preset(db_path, user.id, preset_id)
    if preset is None:
        await _edit_or_send(update, "Preset not found.", InlineKeyboardMarkup([[InlineKeyboardButton("Menu", callback_data="settings:menu")]]))
        return

    cap = "(AI auto-transcribed)"
    style = preset.caption_style or "none"
    color = preset.caption_color or "none"
    pos = preset.caption_position or "none"
    voice = preset.voice_over_voice or "(none)"
    v_text = preset.voice_text or "(none)"
    quality = preset.voice_quality or "none"
    speed = preset.voice_speed if preset.voice_speed is not None else "none"
    tts = preset.tts_engine or "auto"
    banner = preset.banner_path or "(none)"
    b_pos = preset.banner_position or "none"
    b_scale = preset.banner_scale or "none"
    shared = "yes" if preset.shared else "no"

    text = (
        f"Preset: {preset.name}\n"
        f"Caption: {cap}\n"
        f"  Style: {style} | Color: {color} | Position: {pos}\n"
        f"Voice: {voice}\n"
        f"  Voice text: {v_text}\n"
        f"  Quality: {quality} | Speed: {speed} | TTS: {tts}\n"
        f"Banner: {banner}\n"
        f"  Position: {b_pos} | Scale: {b_scale}\n"
        f"Shared: {shared}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Caption color", callback_data=f"preset:field:{preset.id}:caption_color")],
        [InlineKeyboardButton("Caption style", callback_data=f"preset:field:{preset.id}:caption_style")],
        [InlineKeyboardButton("Caption position", callback_data=f"preset:field:{preset.id}:caption_position")],
        [InlineKeyboardButton("Voice name", callback_data=f"preset:field:{preset.id}:voice_over_voice")],
        [InlineKeyboardButton("Voice text", callback_data=f"preset:field:{preset.id}:voice_text")],
        [InlineKeyboardButton("Voice quality", callback_data=f"preset:field:{preset.id}:voice_quality")],
        [InlineKeyboardButton("Voice speed", callback_data=f"preset:field:{preset.id}:voice_speed")],
        [InlineKeyboardButton("TTS engine", callback_data=f"preset:field:{preset.id}:tts_engine")],
        [InlineKeyboardButton("Banner image", callback_data=f"preset:field:{preset.id}:banner_path")],
        [InlineKeyboardButton("Banner position", callback_data=f"preset:field:{preset.id}:banner_position")],
        [InlineKeyboardButton("Banner scale", callback_data=f"preset:field:{preset.id}:banner_scale")],
        [InlineKeyboardButton("Share", callback_data=f"preset:share:{preset.id}")],
        [InlineKeyboardButton("Delete", callback_data=f"preset:delete:{preset.id}")],
        [InlineKeyboardButton("← Back", callback_data="preset:back")],
    ])
    await _edit_or_send(update, text, keyboard)


async def _start_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE, preset_id: int, field_name: str) -> None:
    flow: FlowState = context.user_data["settings_flow"]
    flow.action = _State.PRESET_EDIT_FIELD
    flow.data["preset_id"] = preset_id
    flow.data["field_name"] = field_name
    pretty = field_name.replace("_", " ").title()
    await _edit_message(update.callback_query, f"Send new value for {pretty} (or /skip to clear):")


async def _apply_preset_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, preset_id: int, field_name: str, value: Any) -> None:
    db_path: Path = context.application.bot_data["db_path"]
    user = update.effective_user
    if user is None:
        return
    updated = await update_preset(db_path, preset_id, user.id, **{field_name: value})
    if updated is None:
        await update.message.reply_text("Preset not found.")
        return
    flow: FlowState = context.user_data["settings_flow"]
    flow.action = _State.PRESET_EDIT
    await update.message.reply_text("Updated. Open the preset to see changes.")
    await _show_preset_edit(update, context, preset_id)


async def _delete_preset_and_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE, preset_id: int) -> None:
    db_path: Path = context.application.bot_data["db_path"]
    user = update.effective_user
    if user is None:
        return
    deleted = await delete_preset(db_path, user.id, preset_id)
    if not deleted:
        await update.callback_query.answer("Not found", show_alert=True)
        return
    await update.callback_query.answer("Deleted")
    context.user_data["settings_flow"].action = _State.PRESET_LIST
    context.user_data["settings_flow"].page = 0
    await _show_preset_list(update, context)


async def _share_preset(update: Update, context: ContextTypes.DEFAULT_TYPE, preset_id: int) -> None:
    db_path: Path = context.application.bot_data["db_path"]
    user = update.effective_user
    if user is None:
        return
    code = await share_preset(db_path, preset_id, user.id)
    if code is None:
        await update.callback_query.answer("Could not share", show_alert=True)
        return
    await update.callback_query.answer("Shared")
    await _edit_or_send(update, f"Share code: {code}\nOthers can import this preset in their settings.", None)


async def _finalize_preset_create(update: Update, context: ContextTypes.DEFAULT_TYPE, db_path: Path) -> None:
    user = update.effective_user
    if user is None:
        return
    flow: FlowState = context.user_data["settings_flow"]

    banner_url = flow.data.pop("banner_url", None)
    if banner_url:
        await update.message.reply_text("Downloading banner image...")
        import urllib.request
        storage_dir: Path = context.application.bot_data.get("storage_dir", Path("runtime/jobs"))
        banner_dest = storage_dir / f"banner_{user.id}_{flow.data['name']}.png"
        try:
            urllib.request.urlretrieve(banner_url, banner_dest)
            flow.data["banner_path"] = str(banner_dest)
        except Exception as exc:
            LOGGER.warning("Banner download failed: %s", exc)
            await update.message.reply_text(f"Banner download failed: {exc}. Preset created without banner.")

    preset = await create_preset(db_path, user.id, flow.data["name"], **flow.data)
    flow.action = _State.PRESET_LIST
    await update.message.reply_text(f"Preset \"{preset.name}\" created.")
    await _show_preset_list(update, context)


async def _show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db_path: Path = context.application.bot_data["db_path"]
    user = update.effective_user
    if user is None:
        return
    jobs = await list_user_jobs(db_path, user.id, limit=50)
    total = len(jobs)
    uploaded = sum(1 for j in jobs if j.status == "uploaded")
    failed = sum(1 for j in jobs if j.status == "failed")
    size = sum(j.file_size or 0 for j in jobs)
    size_mb = size / (1024 * 1024)

    text = (
        f"Stats:\n"
        f"Downloads: {total}\n"
        f"Uploaded: {uploaded}\n"
        f"Failed: {failed}\n"
        f"Total size: {size_mb:.1f} MB\n\n"
        "Recent:\n"
    )
    for j in jobs[:10]:
        text += f"[{j.status}] #{j.id}: {j.url[:50]}... ({_fmt_size(j)})\n"

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Menu", callback_data="settings:menu")]])
    await _edit_or_send(update, text, keyboard)


async def _show_edit_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow: FlowState = context.user_data["settings_flow"]
    db_path: Path = context.application.bot_data["db_path"]
    user = update.effective_user
    if user is None:
        return
    jobs = await list_source_jobs_for_user(db_path, user.id, limit=50)
    total = len(jobs)
    start = flow.page * _PAGE_SIZE
    page = jobs[start:start + _PAGE_SIZE]

    rows = []
    for j in page:
        label = f"#{j.id} {j.url[:40]}..."
        rows.append([InlineKeyboardButton(label, callback_data=f"edit:source:{j.id}")])

    nav = []
    if flow.page > 0:
        nav.append(InlineKeyboardButton("← Back", callback_data=f"edit:page:{flow.page - 1}"))
    if start + _PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Forward →", callback_data=f"edit:page:{flow.page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("Menu", callback_data="settings:menu")])

    text = "Select a video to edit:" if total > 0 else "No videos available."
    await _edit_or_send(update, text, InlineKeyboardMarkup(rows))


async def _show_edit_preset_select(update: Update, context: ContextTypes.DEFAULT_TYPE, source_job_id: int) -> None:
    db_path: Path = context.application.bot_data["db_path"]
    user = update.effective_user
    if user is None:
        return
    source = await get_job(db_path, source_job_id)
    if source is None or source.file_path is None:
        await _edit_or_send(update, "Source video not found.", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="settings:edit_source")]]))
        return
    presets = await list_presets(db_path, user.id)
    rows = []
    for p in presets:
        rows.append([InlineKeyboardButton(p.name, callback_data=f"edit:preset:{p.id}")])
    rows.append([InlineKeyboardButton("Temp config (no preset)", callback_data="edit:use_temp")])
    rows.append([InlineKeyboardButton("← Back", callback_data="settings:edit_source")])

    keyboard = InlineKeyboardMarkup(rows)
    text = f"Editing video #{source_job_id}\nChoose a preset or use a temporary config:"
    await _edit_or_send(update, text, keyboard)


async def _start_edit_process(update: Update, context: ContextTypes.DEFAULT_TYPE, preset_id: int) -> None:
    db_path: Path = context.application.bot_data["db_path"]
    storage_dir: Path = context.application.bot_data["storage_dir"]
    flow: FlowState = context.user_data["settings_flow"]
    source_job_id = flow.data.get("source_job_id")
    user = update.effective_user
    if user is None or source_job_id is None:
        return
    source = await get_job(db_path, source_job_id)
    if source is None or source.file_path is None:
        await update.callback_query.answer("Source not found", show_alert=True)
        return
    source_path = Path(source.file_path)
    if not source_path.is_file():
        await update.callback_query.answer("Source file missing", show_alert=True)
        return

    edit = await create_edit_job(db_path, source_job_id, user.id, preset_id)
    dest = storage_dir / f"edit-{edit.id}-{source_path.name}"
    import shutil
    shutil.copy2(source_path, dest)
    await update_edit_job(db_path, edit.id, file_path=str(dest), file_size=dest.stat().st_size)
    await update.callback_query.answer("Edit job created")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Add to Pool", callback_data=f"edit:pool:{edit.id}")],
        [InlineKeyboardButton("Edit Config", callback_data=f"edit:config:{edit.id}")],
    ])
    await _edit_or_send(update, f"Edit job #{edit.id} created from source #{source_job_id}.\nRendering with your preset...", keyboard)
    context.user_data["settings_flow"].action = _State.EDIT_PROCESSING
    context.user_data["settings_flow"].data = {"edit_id": edit.id, "source_job_id": source_job_id}


async def _start_temp_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db_path: Path = context.application.bot_data["db_path"]
    storage_dir: Path = context.application.bot_data["storage_dir"]
    flow: FlowState = context.user_data["settings_flow"]
    source_job_id = flow.data.get("source_job_id")
    user = update.effective_user
    if user is None or source_job_id is None:
        return
    source = await get_job(db_path, source_job_id)
    if source is None or source.file_path is None:
        await update.callback_query.answer("Source not found", show_alert=True)
        return
    source_path = Path(source.file_path)
    if not source_path.is_file():
        await update.callback_query.answer("Source file missing", show_alert=True)
        return
    edit = await create_edit_job(db_path, source_job_id, user.id, preset_id=None)
    dest = storage_dir / f"edit-{edit.id}-{source_path.name}"
    import shutil
    shutil.copy2(source_path, dest)
    await update_edit_job(db_path, edit.id, file_path=str(dest), file_size=dest.stat().st_size)
    await update.callback_query.answer("Edit job created")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Add to Pool", callback_data=f"edit:pool:{edit.id}")],
        [InlineKeyboardButton("Edit Config", callback_data=f"edit:config:{edit.id}")],
    ])
    await _edit_or_send(update, f"Edit job #{edit.id} created.\nUse /editconfig to set options for this edit.", keyboard)
    context.user_data["settings_flow"].action = _State.EDIT_PROCESSING
    context.user_data["settings_flow"].data = {"edit_id": edit.id, "source_job_id": source_job_id}


async def _edit_message(query, text: str, reply_markup=None) -> None:
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception:
        pass


async def _edit_or_send(update: Update, text: str, reply_markup=None) -> None:
    if update.callback_query:
        await _edit_message(update.callback_query, text, reply_markup)
    elif update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)


async def _get_preset(db_path: Path, user_id: int, preset_id: int) -> Preset | None:
    from .storage import list_presets
    for p in await list_presets(db_path, user_id):
        if p.id == preset_id:
            return p
    return None


def _fmt_size(j: JobRecord) -> str:
    return f"{j.file_size / 1024 / 1024:.1f} MB" if j.file_size else "?"


async def _add_edit_to_pool(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_id: int) -> None:
    db_path: Path = context.application.bot_data["db_path"]
    storage_dir: Path = context.application.bot_data["storage_dir"]
    user = update.effective_user
    if user is None:
        return
    edit = await get_edit_job(db_path, edit_id)
    if edit is None or edit.file_path is None:
        await update.callback_query.answer("Edit not found", show_alert=True)
        return
    pool_item = await create_pool_item(db_path, user.id, edit.file_path, source_job_id=edit.source_job_id, title=None)
    await update.callback_query.answer("Added to pool")
    await _edit_or_send(update, f"Edit job #{edit_id} added to pool as item #{pool_item.id}.", None)


async def _start_editconfig_from_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_id: int) -> None:
    db_path: Path = context.application.bot_data["db_path"]
    edit = await get_edit_job(db_path, edit_id)
    if edit is None:
        await update.callback_query.answer("Edit not found", show_alert=True)
        return
    flow = context.user_data.get("settings_flow")
    if flow is None:
        flow = {"action": "editconfig", "edit_id": edit_id, "source_job_id": edit.source_job_id}
        context.user_data["settings_flow"] = flow
    else:
        flow["action"] = "editconfig"
        flow["edit_id"] = edit_id
        flow["source_job_id"] = edit.source_job_id
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Caption color", callback_data="editcfg:caption_color")],
        [InlineKeyboardButton("Caption style", callback_data="editcfg:caption_style")],
        [InlineKeyboardButton("Caption position", callback_data="editcfg:caption_position")],
        [InlineKeyboardButton("Voice name", callback_data="editcfg:voice_over_voice")],
        [InlineKeyboardButton("Voice text", callback_data="editcfg:voice_text")],
        [InlineKeyboardButton("Voice quality", callback_data="editcfg:voice_quality")],
        [InlineKeyboardButton("Voice speed", callback_data="editcfg:voice_speed")],
        [InlineKeyboardButton("TTS engine", callback_data="editcfg:tts_engine")],
        [InlineKeyboardButton("Banner image URL", callback_data="editcfg:banner_path")],
        [InlineKeyboardButton("Banner position", callback_data="editcfg:banner_position")],
        [InlineKeyboardButton("Banner scale", callback_data="editcfg:banner_scale")],
        [InlineKeyboardButton("Render now", callback_data="editcfg:render")],
    ])
    await _edit_or_send(update, f"Edit job #{edit_id}\nChoose an option:", keyboard)
