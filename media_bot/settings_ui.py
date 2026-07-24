from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, PhotoSize, Update
from telegram.ext import ContextTypes

from .colors import COLOR_HUES, color_emoji, color_label, shade_options
from .storage import (
    EditJob,
    JobRecord,
    Preset,
    create_edit_job,
    create_pool_item,
    create_preset,
    delete_preset,
    get_edit_job,
    get_job,
    get_or_create_user_settings,
    list_presets,
    list_source_jobs_for_user,
    list_user_jobs,
    share_preset,
    update_edit_job,
    update_preset,
    update_user_settings,
)

LOGGER = logging.getLogger(__name__)

_PAGE_SIZE = 6


class _State:
    MENU = "settings_menu"
    PRESET_LIST = "preset_list"
    PRESET_CREATE_NAME = "preset_create_name"
    PRESET_CREATE_CAPTION = "preset_create_caption"
    PRESET_CREATE_CAPTION_COLOR = "preset_create_caption_color"
    PRESET_CREATE_CAPTION_HUE = "preset_create_caption_hue"
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
    PRESET_CREATE_WATERMARK = "preset_create_watermark"
    PRESET_CREATE_WATERMARK_POS = "preset_create_watermark_pos"
    PRESET_CREATE_CHANNEL_BANNER = "preset_create_channel_banner"
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


_FIELD_CHOICES: dict[str, list[tuple[str, str]]] = {
    "caption_style": [("✍️ Basic", "basic"), ("💪 Bold", "bold"), ("💬 Bubble", "bubble")],
    "caption_position": [("⬇️ Low", "low"), ("↔️ Middle", "middle"), ("⬆️ High", "high")],
    "voice_quality": [("📶 Basic", "basic"), ("✨ Premium", "premium")],
    "tts_engine": [("🔊 edge-tts", "edge-tts"), ("🗣️ espeak-ng", "espeak-ng"), ("🤖 Auto", "auto")],
    "banner_position": [("⬆️ Top", "top"), ("⬇️ Bottom", "bottom"), ("🖼️ Overlay", "overlay")],
    "banner_scale": [("📐 Fit", "fit"), ("↔️ Stretch", "stretch"), ("⬜ Fill", "fill")],
    "watermark_position": [
        ("🪄 Auto", "auto"),
        ("↖️ Top-left", "top-left"),
        ("↗️ Top-right", "top-right"),
        ("↙️ Bottom-left", "bottom-left"),
        ("↘️ Bottom-right", "bottom-right"),
        ("🎯 Center", "center"),
    ],
    "watermark_removal": [("✅ Yes", "yes"), ("❌ No", "no")],
    "channel_banner": [("✅ Yes", "yes"), ("❌ No", "no")],
    "auto_captions": [("✅ Yes", "yes"), ("❌ No", "no")],
}

_TEXT_FIELDS = frozenset({
    "voice_over_voice", "voice_text", "caption_text", "banner_path", "voice_speed",
})

_BOOL_FIELDS = frozenset({"watermark_removal", "channel_banner", "auto_captions"})


def _fmt_current(value: Any, *, color: bool = False) -> str:
    if value is None or value == "":
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if color:
        return f"{color_emoji(str(value))} {color_label(str(value))}"
    text = str(value)
    if len(text) > 18:
        return text[:15] + "…"
    return text


def _coerce_choice_value(field: str, raw: str) -> Any:
    if field in _BOOL_FIELDS:
        return raw.lower() in ("yes", "y", "on", "true", "1")
    if field == "voice_speed":
        return float(raw)
    return raw


def _color_hue_keyboard(back_data: str, hue_prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{emoji} {name}", callback_data=f"{hue_prefix}:{key}")]
        for key, name, emoji in COLOR_HUES
    ]
    rows.append([InlineKeyboardButton("← Back", callback_data=back_data)])
    return InlineKeyboardMarkup(rows)


def _color_shade_keyboard(hue: str, back_data: str, set_prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(label, callback_data=f"{set_prefix}:{value}")]
        for value, label in shade_options(hue)
    ]
    rows.append([InlineKeyboardButton("← Back", callback_data=back_data)])
    return InlineKeyboardMarkup(rows)


def _choice_keyboard(field: str, back_data: str, set_prefix: str) -> InlineKeyboardMarkup:
    options = _FIELD_CHOICES.get(field, [])
    rows = [
        [InlineKeyboardButton(label, callback_data=f"{set_prefix}:{value}")]
        for label, value in options
    ]
    rows.append([InlineKeyboardButton("← Back", callback_data=back_data)])
    return InlineKeyboardMarkup(rows)


def _config_snapshot(cfg: Preset | EditJob) -> dict[str, Any]:
    return {
        "caption_color": cfg.caption_color,
        "caption_style": cfg.caption_style,
        "caption_position": cfg.caption_position,
        "voice_over_voice": cfg.voice_over_voice,
        "voice_text": cfg.voice_text,
        "voice_quality": cfg.voice_quality,
        "voice_speed": cfg.voice_speed,
        "tts_engine": cfg.tts_engine,
        "banner_path": cfg.banner_path,
        "banner_position": cfg.banner_position,
        "banner_scale": cfg.banner_scale,
        "watermark_removal": cfg.watermark_removal,
        "watermark_position": cfg.watermark_position,
        "channel_banner": cfg.channel_banner,
    }


def _build_config_rows(
    values: dict[str, Any],
    *,
    field_prefix: str,
    include_watermark_position: bool = False,
) -> list[list[InlineKeyboardButton]]:
    color = _fmt_current(values.get("caption_color"), color=True)
    style = _fmt_current(values.get("caption_style"))
    pos = _fmt_current(values.get("caption_position"))
    voice = _fmt_current(values.get("voice_over_voice"))
    v_text = _fmt_current(values.get("voice_text"))
    quality = _fmt_current(values.get("voice_quality"))
    speed = _fmt_current(values.get("voice_speed"))
    tts = _fmt_current(values.get("tts_engine"))
    banner = _fmt_current(values.get("banner_path"))
    b_pos = _fmt_current(values.get("banner_position"))
    b_scale = _fmt_current(values.get("banner_scale"))
    wm = _fmt_current(values.get("watermark_removal"))
    ch = _fmt_current(values.get("channel_banner"))

    rows = [
        [InlineKeyboardButton(f"🎨 Caption Colour [{color}]", callback_data=f"{field_prefix}:caption_color")],
        [InlineKeyboardButton(f"✍️ Caption Style [{style}]", callback_data=f"{field_prefix}:caption_style")],
        [InlineKeyboardButton(f"📍 Caption Position [{pos}]", callback_data=f"{field_prefix}:caption_position")],
        [InlineKeyboardButton(f"🗣️ Voice Name [{voice}]", callback_data=f"{field_prefix}:voice_over_voice")],
        [InlineKeyboardButton(f"📝 Voice Text [{v_text}]", callback_data=f"{field_prefix}:voice_text")],
        [InlineKeyboardButton(f"✨ Voice Quality [{quality}]", callback_data=f"{field_prefix}:voice_quality")],
        [InlineKeyboardButton(f"⏩ Voice Speed [{speed}]", callback_data=f"{field_prefix}:voice_speed")],
        [InlineKeyboardButton(f"🔊 TTS Engine [{tts}]", callback_data=f"{field_prefix}:tts_engine")],
        [InlineKeyboardButton(f"🖼️ Banner Image [{banner}]", callback_data=f"{field_prefix}:banner_path")],
        [InlineKeyboardButton(f"📌 Banner Position [{b_pos}]", callback_data=f"{field_prefix}:banner_position")],
        [InlineKeyboardButton(f"📏 Banner Scale [{b_scale}]", callback_data=f"{field_prefix}:banner_scale")],
        [InlineKeyboardButton(f"🚫 Remove Watermark [{wm}]", callback_data=f"{field_prefix}:watermark_removal")],
    ]
    if include_watermark_position:
        wm_pos = _fmt_current(values.get("watermark_position"))
        rows.append([InlineKeyboardButton(
            f"🧭 Watermark Position [{wm_pos}]",
            callback_data=f"{field_prefix}:watermark_position",
        )])
    rows.append([InlineKeyboardButton(f"📺 Channel Banner [{ch}]", callback_data=f"{field_prefix}:channel_banner")])
    return rows


async def _show_options(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, prompt: str, field: str, *options: tuple[str, str]) -> None:
    rows = []
    for label, value in options:
        rows.append([InlineKeyboardButton(label, callback_data=f"preset_create:{field}:{value}")])
    rows.append([InlineKeyboardButton("← Back", callback_data="preset_create:back")])
    keyboard = InlineKeyboardMarkup(rows)
    if update.message:
        await update.message.reply_text(prompt, reply_markup=keyboard)
    elif update.callback_query:
        await _edit_message(update.callback_query, prompt, keyboard)


async def _show_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, next_action: str, prompt: str) -> None:
    rows = [
        [InlineKeyboardButton("✅ Yes", callback_data=f"preset_create:yes:{next_action}"),
         InlineKeyboardButton("❌ No", callback_data=f"preset_create:no:{next_action}")],
        [InlineKeyboardButton("← Back", callback_data="preset_create:back")],
    ]
    keyboard = InlineKeyboardMarkup(rows)
    if update.message:
        await update.message.reply_text(prompt, reply_markup=keyboard)
    elif update.callback_query:
        await _edit_message(update.callback_query, prompt, keyboard)


async def _resume_preset_create_step(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: FlowState, query) -> None:
    """Re-show the UI for the current create-wizard step (used by step-back)."""
    action = flow.action
    if action == _State.PRESET_CREATE_CAPTION_COLOR:
        await _edit_message(
            query,
            "Caption colour:",
            _color_hue_keyboard("preset_create:cancel", "preset_create:hue"),
        )
    elif action == _State.PRESET_CREATE_CAPTION_STYLE:
        await _show_options(update, context, action, "Caption style:", "caption_style",
            *_FIELD_CHOICES["caption_style"])
    elif action == _State.PRESET_CREATE_CAPTION_POS:
        await _show_options(update, context, action, "Caption position:", "caption_position",
            *_FIELD_CHOICES["caption_position"])
    elif action == _State.PRESET_CREATE_VOICE:
        await _edit_message(query, "Send voice-over voice name (or /skip for none):\n← Use Back to return")
    elif action == _State.PRESET_CREATE_VOICE_TEXT:
        await _edit_message(query, "Voice-over text to speak (or /skip for none):")
    elif action == _State.PRESET_CREATE_VOICE_QUALITY:
        await _show_options(update, context, action, "Voice quality:", "voice_quality",
            *_FIELD_CHOICES["voice_quality"])
    elif action == _State.PRESET_CREATE_VOICE_SPEED:
        await _edit_message(query, "Voice speed? (0.5 to 2.0, e.g. 1.0):")
    elif action == _State.PRESET_CREATE_TTS_ENGINE:
        await _show_options(update, context, action, "TTS engine:", "tts_engine",
            *_FIELD_CHOICES["tts_engine"])
    elif action == _State.PRESET_CREATE_BANNER:
        await _edit_message(query, "Add a banner/watermark? Send a photo, paste an image URL, or /skip:")
    elif action == _State.PRESET_CREATE_BANNER_POS:
        await _show_options(update, context, action, "Banner position:", "banner_position",
            *_FIELD_CHOICES["banner_position"])
    elif action == _State.PRESET_CREATE_BANNER_SCALE:
        await _show_options(update, context, action, "Banner scale:", "banner_scale",
            *_FIELD_CHOICES["banner_scale"])
    elif action == _State.PRESET_CREATE_WATERMARK:
        await _show_confirm(update, context, action, "Remove watermark?")
    elif action == _State.PRESET_CREATE_WATERMARK_POS:
        await _show_options(update, context, action, "Watermark position:", "watermark_position",
            *_FIELD_CHOICES["watermark_position"])
    elif action == _State.PRESET_CREATE_CHANNEL_BANNER:
        await _show_confirm(update, context, action, "Channel banner for landscape videos?")
    else:
        await _edit_message(query, "Cancelled preset creation.")
        flow.action = _State.MENU
        flow.data.clear()
        await _show_menu(update, context)


_CREATE_PREV_STEP: dict[str, str | None] = {
    _State.PRESET_CREATE_CAPTION_COLOR: None,
    _State.PRESET_CREATE_CAPTION_HUE: _State.PRESET_CREATE_CAPTION_COLOR,
    _State.PRESET_CREATE_CAPTION_STYLE: _State.PRESET_CREATE_CAPTION_COLOR,
    _State.PRESET_CREATE_CAPTION_POS: _State.PRESET_CREATE_CAPTION_STYLE,
    _State.PRESET_CREATE_VOICE: _State.PRESET_CREATE_CAPTION_POS,
    _State.PRESET_CREATE_VOICE_TEXT: _State.PRESET_CREATE_VOICE,
    _State.PRESET_CREATE_VOICE_QUALITY: _State.PRESET_CREATE_VOICE_TEXT,
    _State.PRESET_CREATE_VOICE_SPEED: _State.PRESET_CREATE_VOICE_QUALITY,
    _State.PRESET_CREATE_TTS_ENGINE: _State.PRESET_CREATE_VOICE_SPEED,
    _State.PRESET_CREATE_BANNER: _State.PRESET_CREATE_TTS_ENGINE,
    _State.PRESET_CREATE_BANNER_POS: _State.PRESET_CREATE_BANNER,
    _State.PRESET_CREATE_BANNER_SCALE: _State.PRESET_CREATE_BANNER_POS,
    _State.PRESET_CREATE_WATERMARK: _State.PRESET_CREATE_BANNER,
    _State.PRESET_CREATE_WATERMARK_POS: _State.PRESET_CREATE_WATERMARK,
    _State.PRESET_CREATE_CHANNEL_BANNER: _State.PRESET_CREATE_WATERMARK,
}


async def _handle_preset_create_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: FlowState, query) -> None:
    data = query.data
    if data in ("preset_create:back", "preset_create:cancel"):
        if data == "preset_create:cancel" or _CREATE_PREV_STEP.get(flow.action) is None:
            await _edit_message(query, "Cancelled preset creation.")
            flow.action = _State.MENU
            flow.data.clear()
            await _show_menu(update, context)
            return
        prev = _CREATE_PREV_STEP[flow.action]
        if flow.action == _State.PRESET_CREATE_CHANNEL_BANNER and flow.data.get("watermark_removal"):
            prev = _State.PRESET_CREATE_WATERMARK_POS
        if flow.action == _State.PRESET_CREATE_WATERMARK and (
            flow.data.get("banner_path") or flow.data.get("banner_url")
        ):
            prev = _State.PRESET_CREATE_BANNER_SCALE
        # Clear fields for the step we're leaving
        if flow.action == _State.PRESET_CREATE_CAPTION_STYLE:
            flow.data.pop("caption_style", None)
        elif flow.action == _State.PRESET_CREATE_CAPTION_POS:
            flow.data.pop("caption_position", None)
        elif flow.action == _State.PRESET_CREATE_VOICE:
            pass
        elif flow.action == _State.PRESET_CREATE_BANNER_POS:
            flow.data.pop("banner_position", None)
        elif flow.action == _State.PRESET_CREATE_BANNER_SCALE:
            flow.data.pop("banner_scale", None)
        elif flow.action == _State.PRESET_CREATE_WATERMARK:
            flow.data.pop("watermark_removal", None)
        elif flow.action == _State.PRESET_CREATE_WATERMARK_POS:
            flow.data.pop("watermark_position", None)
        elif flow.action == _State.PRESET_CREATE_CHANNEL_BANNER:
            flow.data.pop("channel_banner", None)
        if prev == _State.PRESET_CREATE_CAPTION_COLOR:
            flow.data.pop("caption_color", None)
            flow.data.pop("pending_hue", None)
        if prev == _State.PRESET_CREATE_BANNER:
            flow.data.pop("banner_path", None)
            flow.data.pop("banner_url", None)
            flow.data.pop("banner_position", None)
            flow.data.pop("banner_scale", None)
        flow.action = prev
        await _resume_preset_create_step(update, context, flow, query)
        return

    if data.startswith("preset_create:hue:"):
        hue = data.split(":")[-1]
        flow.action = _State.PRESET_CREATE_CAPTION_HUE
        flow.data["pending_hue"] = hue
        await _edit_message(
            query,
            f"Pick a shade of {color_label(hue)}:",
            _color_shade_keyboard(hue, "preset_create:color_back", "preset_create:caption_color"),
        )
        return

    if data == "preset_create:color_back":
        flow.action = _State.PRESET_CREATE_CAPTION_COLOR
        await _edit_message(
            query,
            "Caption colour:",
            _color_hue_keyboard("preset_create:cancel", "preset_create:hue"),
        )
        return

    parts = data.split(":", 2)
    if len(parts) < 3:
        return
    prefix, field, value = parts

    if field == "yes":
        await _handle_yesno(update, context, flow, query, next_action=value, choice=True)
        return
    if field == "no":
        await _handle_yesno(update, context, flow, query, next_action=value, choice=False)
        return

    flow.data[field] = value

    if field == "caption_color":
        flow.data.pop("pending_hue", None)
        flow.action = _State.PRESET_CREATE_CAPTION_STYLE
        await _show_options(update, context, flow.action, "Caption style:", "caption_style",
            *_FIELD_CHOICES["caption_style"])
    elif field == "caption_style":
        flow.action = _State.PRESET_CREATE_CAPTION_POS
        await _show_options(update, context, flow.action, "Caption position:", "caption_position",
            *_FIELD_CHOICES["caption_position"])
    elif field == "caption_position":
        flow.action = _State.PRESET_CREATE_VOICE
        await _edit_message(query, "Send voice-over voice name (or /skip for none):")
    elif field == "voice_quality":
        flow.action = _State.PRESET_CREATE_VOICE_SPEED
        await _edit_message(query, "Voice speed? (0.5 to 2.0, e.g. 1.0):")
    elif field == "tts_engine":
        flow.action = _State.PRESET_CREATE_BANNER
        await _edit_message(query, "Add a banner/watermark? Send a photo, paste an image URL, or /skip:")
    elif field == "banner_position":
        flow.action = _State.PRESET_CREATE_BANNER_SCALE
        await _show_options(update, context, flow.action, "Banner scale:", "banner_scale",
            *_FIELD_CHOICES["banner_scale"])
    elif field == "banner_scale":
        flow.action = _State.PRESET_CREATE_WATERMARK
        await _show_confirm(update, context, flow.action, "Remove watermark?")
    elif field == "watermark_position":
        flow.action = _State.PRESET_CREATE_CHANNEL_BANNER
        await _show_confirm(update, context, flow.action, "Channel banner for landscape videos?")
    elif field == "watermark_removal":
        val = value == "yes"
        flow.data["watermark_removal"] = val
        if val:
            flow.action = _State.PRESET_CREATE_WATERMARK_POS
            await _show_options(update, context, flow.action, "Watermark position:", "watermark_position",
                *_FIELD_CHOICES["watermark_position"])
        else:
            flow.action = _State.PRESET_CREATE_CHANNEL_BANNER
            await _show_confirm(update, context, flow.action, "Channel banner for landscape videos?")
    elif field == "channel_banner":
        flow.data["channel_banner"] = value == "yes"
        await _finalize_preset_create(update, context, context.application.bot_data["db_path"])


async def _handle_yesno(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: FlowState, query, next_action: str, choice: bool) -> None:
    if next_action == str(_State.PRESET_CREATE_WATERMARK):
        flow.data["watermark_removal"] = choice
        if choice:
            flow.action = _State.PRESET_CREATE_WATERMARK_POS
            await _show_options(update, context, flow.action, "Watermark position:", "watermark_position",
                *_FIELD_CHOICES["watermark_position"])
        else:
            flow.action = _State.PRESET_CREATE_CHANNEL_BANNER
            await _show_confirm(update, context, flow.action, "Channel banner for landscape videos?")
    elif next_action == str(_State.PRESET_CREATE_CHANNEL_BANNER):
        flow.data["channel_banner"] = choice
        await _finalize_preset_create(update, context, context.application.bot_data["db_path"])


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

    if query.data.startswith("preset_create:"):
        await _handle_preset_create_callback(update, context, flow, query)
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

    if query.data.startswith("preset:hue:"):
        # preset:hue:{id}:{hue}
        parts = query.data.split(":")
        preset_id = int(parts[2])
        hue = parts[3]
        await _edit_message(
            query,
            f"Pick a shade of {color_label(hue)}:",
            _color_shade_keyboard(
                hue,
                f"preset:field:{preset_id}:caption_color",
                f"preset:set:{preset_id}:caption_color",
            ),
        )
        return

    if query.data.startswith("preset:set:"):
        # preset:set:{id}:{field}:{value}
        parts = query.data.split(":", 4)
        if len(parts) < 5:
            return
        preset_id = int(parts[2])
        field_name = parts[3]
        raw_value = parts[4]
        value = _coerce_choice_value(field_name, raw_value)
        await _apply_preset_edit_callback(update, context, preset_id, field_name, value)
        return

    if query.data.startswith("preset:active:"):
        preset_id = int(query.data.split(":")[-1])
        await _set_active_preset(update, context, preset_id)
        return

    if query.data.startswith("preset:menu:"):
        preset_id = int(query.data.split(":")[-1])
        flow.action = _State.PRESET_EDIT
        flow.data["preset_id"] = preset_id
        await _show_preset_edit(update, context, preset_id)
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
        await _show_options(
            update, context, flow.action,
            "Banner position:", "banner_position",
            *_FIELD_CHOICES["banner_position"],
        )
        return True

    return False


async def settings_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    flow = context.user_data.get("settings_flow")
    if flow is None or not update.message or not update.message.text:
        return False
    if not isinstance(flow, FlowState):
        return False

    text = update.message.text.strip()
    db_path: Path = context.application.bot_data["db_path"]
    user = update.effective_user

    if flow.action == _State.PRESET_CREATE_NAME:
        if not text:
            return True
        flow.data["name"] = text
        flow.data["auto_captions"] = True
        flow.action = _State.PRESET_CREATE_CAPTION_COLOR
        await update.message.reply_text(
            "Caption colour:",
            reply_markup=_color_hue_keyboard("preset_create:cancel", "preset_create:hue"),
        )
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
        await _show_options(
            update, context, flow.action,
            "Voice quality:", "voice_quality",
            *_FIELD_CHOICES["voice_quality"],
        )
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
        await _show_options(
            update, context, flow.action,
            "TTS engine:", "tts_engine",
            *_FIELD_CHOICES["tts_engine"],
        )
        return True

    if flow.action == _State.PRESET_CREATE_BANNER:
        if text.lower() != "/skip":
            storage_dir: Path = context.application.bot_data.get("storage_dir", Path("runtime/jobs"))
            banner_file = storage_dir / "banners" / f"banner_{user.id if user else 0}.png"
            if text.lower() in ("profile", "p") and banner_file.is_file():
                flow.data["banner_path"] = str(banner_file)
                flow.action = _State.PRESET_CREATE_BANNER_POS
                await _show_options(
                    update, context, flow.action,
                    "Banner position:", "banner_position",
                    *_FIELD_CHOICES["banner_position"],
                )
            elif text.startswith("http://") or text.startswith("https://"):
                flow.data["banner_url"] = text
                flow.action = _State.PRESET_CREATE_BANNER_POS
                await _show_options(
                    update, context, flow.action,
                    "Banner position:", "banner_position",
                    *_FIELD_CHOICES["banner_position"],
                )
            else:
                await update.message.reply_text("Send a photo, type 'profile', paste an image URL, or /skip:")
            return True
        flow.action = _State.PRESET_CREATE_WATERMARK
        await _show_confirm(update, context, flow.action, "Remove watermark?")
        return True

    if flow.action == _State.PRESET_EDIT_FIELD:
        preset_id = flow.data.get("preset_id")
        field_name = flow.data.get("field_name")
        if preset_id is None or field_name is None:
            return False
        value = None if text.lower() == "/skip" else text
        text_only_fields = {"voice_over_voice", "voice_text", "caption_text", "banner_path", "voice_speed"}
        if field_name in text_only_fields:
            if field_name in ("voice_speed",):
                if value is not None:
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
        [InlineKeyboardButton("📁 My Presets", callback_data="settings:presets")],
        [InlineKeyboardButton("➕ Create Preset", callback_data="settings:create_preset")],
        [InlineKeyboardButton("🖼️ Set Profile Banner", callback_data="settings:profile_banner")],
        [InlineKeyboardButton("📊 Stats / History", callback_data="settings:stats")],
        [InlineKeyboardButton("🎬 Edit Existing Video", callback_data="settings:edit_source")],
    ])
    msg = "⚙️ Settings menu:\nChoose an option below."
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
    settings = await get_or_create_user_settings(db_path, user.id)
    active_name = settings.preset_name
    total = len(presets)
    start = flow.page * _PAGE_SIZE
    page = presets[start:start + _PAGE_SIZE]

    rows = []
    for p in page:
        mark = "⭐ " if active_name and p.name == active_name else ""
        color = color_emoji(p.caption_color)
        label = f"{mark}{p.name} ({color} {p.caption_style or 'none'})"
        rows.append([InlineKeyboardButton(label, callback_data=f"preset:edit:{p.id}")])
    rows.append([InlineKeyboardButton("➕ Create new", callback_data="settings:create_preset")])

    nav = []
    if flow.page > 0:
        nav.append(InlineKeyboardButton("← Back", callback_data=f"preset:page:{flow.page - 1}"))
    if start + _PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Forward →", callback_data=f"preset:page:{flow.page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 Menu", callback_data="settings:menu")])

    text = f"📁 Your presets ({total}):"
    if total == 0:
        text = "No presets yet. Create one to get started."
    await _edit_or_send(update, text, InlineKeyboardMarkup(rows))


async def _show_preset_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, preset_id: int) -> None:
    db_path: Path = context.application.bot_data["db_path"]
    user = update.effective_user
    if user is None:
        return
    preset = await _get_preset(db_path, user.id, preset_id)
    if preset is None:
        await _edit_or_send(update, "Preset not found.", InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="settings:menu")]]))
        return

    settings = await get_or_create_user_settings(db_path, user.id)
    is_active = settings.preset_name == preset.name
    active_label = "⭐ Active preset" if is_active else "☆ Set as Active"

    values = _config_snapshot(preset)
    rows = _build_config_rows(values, field_prefix=f"preset:field:{preset.id}", include_watermark_position=True)
    rows.append([InlineKeyboardButton(f"{active_label}", callback_data=f"preset:active:{preset.id}")])
    rows.append([InlineKeyboardButton("🔗 Share", callback_data=f"preset:share:{preset.id}")])
    rows.append([InlineKeyboardButton("🗑️ Delete", callback_data=f"preset:delete:{preset.id}")])
    rows.append([InlineKeyboardButton("← Back", callback_data="preset:back")])

    status = "⭐ ACTIVE  " if is_active else ""
    text = (
        f"{status}Preset: {preset.name}\n"
        f"Tap a setting to change it. Current values are shown on each button."
    )
    await _edit_or_send(update, text, InlineKeyboardMarkup(rows))


async def _start_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE, preset_id: int, field_name: str) -> None:
    flow: FlowState = context.user_data["settings_flow"]
    flow.action = _State.PRESET_EDIT_FIELD
    flow.data["preset_id"] = preset_id
    flow.data["field_name"] = field_name
    query = update.callback_query
    back_data = f"preset:menu:{preset_id}"

    if field_name == "caption_color":
        await _edit_message(
            query,
            "Caption colour — pick a hue:",
            _color_hue_keyboard(back_data, f"preset:hue:{preset_id}"),
        )
        return

    if field_name in _FIELD_CHOICES:
        pretty = field_name.replace("_", " ").title()
        await _edit_message(
            query,
            f"Choose {pretty}:",
            _choice_keyboard(field_name, back_data, f"preset:set:{preset_id}:{field_name}"),
        )
        return

    pretty = field_name.replace("_", " ").title()
    await _edit_message(
        query,
        f"Send new value for {pretty} (or /skip to clear):",
        InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=back_data)]]),
    )


async def _apply_preset_edit_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, preset_id: int, field_name: str, value: Any,
) -> None:
    db_path: Path = context.application.bot_data["db_path"]
    user = update.effective_user
    if user is None:
        return
    updated = await update_preset(db_path, preset_id, user.id, **{field_name: value})
    if updated is None:
        await update.callback_query.answer("Preset not found", show_alert=True)
        return
    flow: FlowState = context.user_data["settings_flow"]
    flow.action = _State.PRESET_EDIT
    flow.data.pop("field_name", None)
    await _show_preset_edit(update, context, preset_id)


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
    flow.data.pop("field_name", None)
    await _show_preset_edit(update, context, preset_id)


async def _set_active_preset(update: Update, context: ContextTypes.DEFAULT_TYPE, preset_id: int) -> None:
    db_path: Path = context.application.bot_data["db_path"]
    user = update.effective_user
    if user is None:
        return
    preset = await _get_preset(db_path, user.id, preset_id)
    if preset is None:
        await update.callback_query.answer("Preset not found", show_alert=True)
        return
    await update_user_settings(db_path, user.id, preset_name=preset.name)
    await update.callback_query.answer(f"\"{preset.name}\" is now active")
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

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="settings:menu")]])
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
    rows.append([InlineKeyboardButton("🏠 Menu", callback_data="settings:menu")])

    text = "🎬 Select a video to edit:" if total > 0 else "No videos available."
    await _edit_or_send(update, text, InlineKeyboardMarkup(rows))


async def _show_edit_preset_select(update: Update, context: ContextTypes.DEFAULT_TYPE, source_job_id: int) -> None:
    db_path: Path = context.application.bot_data["db_path"]
    user = update.effective_user
    if user is None:
        return
    source = await get_job(db_path, source_job_id)
    if source is None or source.file_path is None:
        await _edit_or_send(update, "Source video not found.", InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="settings:edit_source")]]))
        return
    presets = await list_presets(db_path, user.id)
    settings = await get_or_create_user_settings(db_path, user.id)
    active_name = settings.preset_name
    rows = []
    for p in presets:
        mark = "⭐ " if active_name and p.name == active_name else ""
        color = color_emoji(p.caption_color)
        rows.append([InlineKeyboardButton(
            f"{mark}{color} {p.name}",
            callback_data=f"edit:preset:{p.id}",
        )])
    rows.append([InlineKeyboardButton("⚙️ Temp config (no preset)", callback_data="edit:use_temp")])
    rows.append([InlineKeyboardButton("← Back", callback_data="settings:edit_source")])

    keyboard = InlineKeyboardMarkup(rows)
    text = f"🎬 Editing video #{source_job_id}\nChoose a preset or use a temporary config:"
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
        [InlineKeyboardButton("🏊 Add to Pool", callback_data=f"edit:pool:{edit.id}")],
        [InlineKeyboardButton("⚙️ Edit Config", callback_data=f"edit:config:{edit.id}")],
    ])
    await _edit_or_send(update, f"🎬 Edit job #{edit.id} created from source #{source_job_id}.\nRendering with your preset...", keyboard)
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
        [InlineKeyboardButton("🏊 Add to Pool", callback_data=f"edit:pool:{edit.id}")],
        [InlineKeyboardButton("⚙️ Edit Config", callback_data=f"edit:config:{edit.id}")],
    ])
    await _edit_or_send(
        update,
        f"🎬 Edit job #{edit.id} created.\nTap Edit Config to set options, then Render.",
        keyboard,
    )
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


def build_editconfig_keyboard(edit: EditJob, preset: Preset | None = None) -> InlineKeyboardMarkup:
    values = _config_snapshot(edit)
    if preset is not None:
        preset_vals = _config_snapshot(preset)
        for key, val in list(values.items()):
            if isinstance(val, bool):
                if not val and preset_vals.get(key):
                    values[key] = preset_vals[key]
            elif val is None:
                values[key] = preset_vals.get(key)
    rows = _build_config_rows(values, field_prefix="editcfg", include_watermark_position=True)
    rows.append([InlineKeyboardButton("🎬 Render now", callback_data="editcfg:render")])
    rows.append([InlineKeyboardButton("← Back", callback_data="editcfg:back")])
    return InlineKeyboardMarkup(rows)


async def show_editconfig_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_id: int, *, intro: str | None = None) -> None:
    db_path: Path = context.application.bot_data["db_path"]
    edit = await get_edit_job(db_path, edit_id)
    if edit is None:
        await _edit_or_send(update, "Edit job not found.", None)
        return
    preset = None
    if edit.preset_id:
        preset = await _get_preset(db_path, edit.user_id, edit.preset_id)
    text = intro or f"🎬 Edit job #{edit_id} from source #{edit.source_job_id}\nCurrent settings are shown on each button."
    await _edit_or_send(update, text, build_editconfig_keyboard(edit, preset))


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
    elif isinstance(flow, FlowState):
        flow = {"action": "editconfig", "edit_id": edit_id, "source_job_id": edit.source_job_id}
        context.user_data["settings_flow"] = flow
    else:
        flow["action"] = "editconfig"
        flow["edit_id"] = edit_id
        flow["source_job_id"] = edit.source_job_id
    await show_editconfig_menu(update, context, edit_id)


async def handle_editconfig_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Handle editcfg:* callbacks. Returns 'render' when render was requested."""
    query = update.callback_query
    if query is None or query.data is None:
        return None
    await query.answer()
    db_path: Path = context.application.bot_data["db_path"]
    flow = context.user_data.get("settings_flow")
    if not flow or (isinstance(flow, dict) and flow.get("action") != "editconfig"):
        if isinstance(flow, FlowState) and flow.action == "editconfig":
            pass
        else:
            await query.edit_message_text("No active edit config. Use /editconfig.")
            return None

    if isinstance(flow, FlowState):
        edit_id = flow.data.get("edit_id")
    else:
        edit_id = flow.get("edit_id")

    data = query.data

    if data == "editcfg:back":
        # Leave submenu or return to settings menu when already on main config
        if isinstance(flow, dict) and flow.get("field_name"):
            flow.pop("field_name", None)
            await show_editconfig_menu(update, context, edit_id)
            return None
        await _edit_message(query, "Closed edit config. Use /editconfig to reopen.")
        return None

    if data == "editcfg:menu":
        if isinstance(flow, dict):
            flow.pop("field_name", None)
        await show_editconfig_menu(update, context, edit_id)
        return None

    if data == "editcfg:render":
        return "render"

    if data.startswith("editcfg:hue:"):
        hue = data.split(":")[-1]
        await _edit_message(
            query,
            f"Pick a shade of {color_label(hue)}:",
            _color_shade_keyboard(hue, "editcfg:caption_color", "editcfg:set:caption_color"),
        )
        return None

    if data.startswith("editcfg:set:"):
        # editcfg:set:{field}:{value}
        parts = data.split(":", 3)
        if len(parts) < 4:
            return None
        field_name = parts[2]
        raw_value = parts[3]
        value = _coerce_choice_value(field_name, raw_value)
        await update_edit_job(db_path, edit_id, **{field_name: value})
        if isinstance(flow, dict):
            flow.pop("field_name", None)
        await show_editconfig_menu(update, context, edit_id)
        return None

    field = data.split(":", 1)[1]
    if isinstance(flow, dict):
        flow["field_name"] = field

    if field == "caption_color":
        await _edit_message(
            query,
            "Caption colour — pick a hue:",
            _color_hue_keyboard("editcfg:menu", "editcfg:hue"),
        )
        return None

    if field in _FIELD_CHOICES:
        pretty = field.replace("_", " ").title()
        await _edit_message(
            query,
            f"Choose {pretty}:",
            _choice_keyboard(field, "editcfg:menu", f"editcfg:set:{field}"),
        )
        return None

    pretty = field.replace("_", " ").title()
    await _edit_message(
        query,
        f"Send new value for {pretty} (or /skip to clear):",
        InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="editcfg:menu")]]),
    )
    return None
