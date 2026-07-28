from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .colors import COLOR_HUES, color_emoji, color_label, shade_options
from .menu import Menu
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
    stage_edit_source,
    update_edit_job,
    update_preset,
    update_user_settings,
)

LOGGER = logging.getLogger(__name__)

_PAGE_SIZE = 6
_BANNER_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_MAX_BANNER_IMAGE_BYTES = 20 * 1024 * 1024


class _State:
    MENU = "settings_menu"
    PRESET_LIST = "preset_list"
    PRESET_CREATE_NAME = "preset_create_name"
    PRESET_CREATE_CAPTION = "preset_create_caption"
    PRESET_CREATE_CAPTION_COLOR = "preset_create_caption_color"
    PRESET_CREATE_CAPTION_HUE = "preset_create_caption_hue"
    PRESET_CREATE_CAPTION_STYLE = "preset_create_caption_style"
    PRESET_CREATE_CAPTION_POS = "preset_create_caption_pos"
    PRESET_CREATE_VOICE_MENU = "preset_create_voice_menu"
    PRESET_CREATE_VOICE = "preset_create_voice"
    PRESET_CREATE_VOICE_TEXT = "preset_create_voice_text"
    PRESET_CREATE_VOICE_QUALITY = "preset_create_voice_quality"
    PRESET_CREATE_VOICE_SPEED = "preset_create_voice_speed"
    PRESET_CREATE_TTS_ENGINE = "preset_create_tts_engine"
    PRESET_CREATE_BANNER_MENU = "preset_create_banner_menu"
    PRESET_CREATE_BANNER = "preset_create_banner"
    PRESET_CREATE_BANNER_POS = "preset_create_banner_pos"
    PRESET_CREATE_BANNER_SCALE = "preset_create_banner_scale"
    PRESET_CREATE_BANNER_UPLOAD = "preset_create_banner_upload"
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
    "caption_style": [("✍️ Basic", "basic"), ("💪 Bold", "bold"), ("💬 Bubble", "bubble"), ("🖼️ Border", "border"), ("🎨 Filled", "filled")],
    "caption_position": [("⬆️ Top", "low"), ("↔️ Middle", "middle"), ("⬇️ Bottom", "high")],
    "voice_quality": [("📶 Basic", "basic"), ("✨ Premium", "premium")],
    "voice_speed": [
        ("🐢 0.50×", "0.5"), ("0.75×", "0.75"), ("1.00×", "1.0"),
        ("1.25×", "1.25"), ("1.50×", "1.5"), ("🐇 2.00×", "2.0"),
    ],
    "tts_engine": [("🔊 edge-tts", "edge-tts"), ("🗣️ espeak-ng", "espeak-ng"), ("🤖 Auto", "auto")],
    "banner_position": [("⬆️ Top", "top"), ("⬇️ Bottom", "bottom"), ("🖼️ Overlay", "overlay")],
    "banner_scale": [("⬜ Fill", "fill"), ("📐 Fit", "fit"), ("↔️ Stretch", "stretch")],
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
    "voice_over_voice", "voice_text", "caption_text", "banner_path",
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


async def _show_voice_selector(query, prefix: str, flow, *, back_data: str) -> None:
    common = [
        ("🔊 Aria (en-US, Female)", "en-US-AriaNeural"),
        ("🔊 Guy (en-US, Male)", "en-US-GuyNeural"),
        ("🔊 Jenny (en-US, Female)", "en-US-JennyNeural"),
        ("🔊 Sonia (en-GB, Female)", "en-GB-SoniaNeural"),
        ("🔊 Ryan (en-GB, Male)", "en-GB-RyanNeural"),
        ("🔊 Xiaoxiao (zh-CN, Female)", "zh-CN-XiaoxiaoNeural"),
        ("🔊 default", "default"),
        ("🔊 male", "male"),
        ("🔊 female", "female"),
    ]
    rows = [
        [InlineKeyboardButton(label, callback_data=f"{prefix}:set:voice_over_voice:{value}")]
        for label, value in common
    ]
    rows.append([InlineKeyboardButton("✏️ Custom…", callback_data=f"{prefix}:voice_over_voice_custom")])
    rows.append([InlineKeyboardButton("← Back", callback_data=back_data)])
    await _edit_message(query, "🎤 Choose a voice:", InlineKeyboardMarkup(rows))


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
        "auto_captions": cfg.auto_captions,
        "caption_text": cfg.caption_text,
        "caption_color": cfg.caption_color,
        "caption_style": cfg.caption_style,
        "caption_position": cfg.caption_position,
        "voice_text": cfg.voice_text,
        "voice_over_voice": cfg.voice_over_voice,
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


def _effective_edit_snapshot(edit: EditJob, preset: Preset | None = None) -> dict[str, Any]:
    values = _config_snapshot(edit)
    if preset is not None:
        preset_values = _config_snapshot(preset)
        for key, value in values.items():
            if value is None:
                values[key] = preset_values.get(key)
    return values


def _voice_summary(values: dict[str, Any]) -> str:
    parts = []
    v = values.get("voice_over_voice")
    if v:
        parts.append(f"🎤 {_fmt_current(v)}")
    s = values.get("voice_speed")
    if s:
        parts.append(f"⚡ {s}x")
    q = values.get("voice_quality")
    if q == "premium":
        parts.append("⭐")
    else:
        parts.append("🍃")
    return " ".join(parts) if parts else "none"


def _tts_engine_overview(engine: str) -> str:
    descriptions = {
        "edge-tts": "Edge TTS uses natural Microsoft neural voices and requires network access.",
        "espeak-ng": "eSpeak NG is local and reliable, but sounds more synthetic.",
        "auto": "Auto prefers Edge TTS and falls back to eSpeak NG when needed.",
    }
    detail = descriptions.get(engine, "The configured speech engine will generate narration audio.")
    return (
        f"🎤 Voice settings\n\n"
        f"Current TTS engine: {engine}\n{detail}\n\n"
        "AI visual narration is not available yet, so manual Voice Text is deprecated."
    )


def _voice_menu_keyboard(back_data: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🎤 Voice Name", callback_data=f"{back_data}:voice_over_voice")],
        [InlineKeyboardButton("✨ Voice Quality", callback_data=f"{back_data}:voice_quality")],
        [InlineKeyboardButton("⏩ Voice Speed", callback_data=f"{back_data}:voice_speed")],
        [InlineKeyboardButton("🔊 TTS Engine", callback_data=f"{back_data}:tts_engine")],
        [InlineKeyboardButton("✅ Done", callback_data=f"{back_data}:done")],
    ]
    return InlineKeyboardMarkup(rows)


def _banner_menu_keyboard(back_data: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🖼️ Banner Image", callback_data=f"{back_data}:banner_path")],
        [InlineKeyboardButton("📌 Banner Position", callback_data=f"{back_data}:banner_position")],
        [InlineKeyboardButton("📏 Banner Scale", callback_data=f"{back_data}:banner_scale")],
        [InlineKeyboardButton("✅ Done", callback_data=f"{back_data}:done")],
    ]
    return InlineKeyboardMarkup(rows)


def _text_input_keyboard(back_data: str, skip_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("← Back", callback_data=back_data),
        InlineKeyboardButton("⏭ Skip", callback_data=skip_data),
    ]])


_BANNER_INSTRUCTIONS = (
    "Banner image:\n"
    "• Send a photo to upload it\n"
    "• Paste a direct http(s) image URL\n"
    "• Type `profile` to reuse your profile banner\n"
    "• Tap Skip to leave the banner empty"
)


def _build_config_rows(
    values: dict[str, Any],
    *,
    field_prefix: str,
    toggle_prefix: str,
    include_watermark_position: bool = False,
) -> list[list[InlineKeyboardButton]]:
    cap_text = _fmt_current(values.get("caption_text"))
    color = _fmt_current(values.get("caption_color"), color=True)
    style = _fmt_current(values.get("caption_style"))
    pos = _fmt_current(values.get("caption_position"))
    v_name = _fmt_current(values.get("voice_over_voice") or "default")
    v_quality = _fmt_current(values.get("voice_quality") or "basic")
    b_path = _fmt_current(values.get("banner_path"))
    rows = [
        [InlineKeyboardButton(f"💬 Caption Text [{cap_text}]", callback_data=f"{field_prefix}:caption_text")],
        [InlineKeyboardButton(f"🎨 Caption Colour [{color}]", callback_data=f"{field_prefix}:caption_color")],
        [InlineKeyboardButton(f"✍️ Caption Style [{style}]", callback_data=f"{field_prefix}:caption_style")],
        [InlineKeyboardButton(f"📍 Caption Position [{pos}]", callback_data=f"{field_prefix}:caption_position")],
        [InlineKeyboardButton(
            f"🎤 Voice Settings [{v_name} {v_quality}]",
            callback_data=f"{field_prefix}:voice_menu",
        )],
        [InlineKeyboardButton(f"🖼️ Banner [{b_path}]", callback_data=f"{field_prefix}:banner_menu")],
    ]
    if include_watermark_position:
        wm_pos = _fmt_current(values.get("watermark_position"))
        rows.append([InlineKeyboardButton(
            f"🧭 Watermark Position [{wm_pos}]",
            callback_data=f"{field_prefix}:watermark_position",
        )])
    toggles = Menu()
    for label, field in (
        ("Auto Captions", "auto_captions"),
        ("Remove Watermark", "watermark_removal"),
        ("Channel Banner", "channel_banner"),
    ):
        enabled = bool(values.get(field))
        toggles.toggle(
            label,
            enabled,
            f"{toggle_prefix}:{field}:{'no' if enabled else 'yes'}",
        )
    rows.extend(toggles.rows)
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
    elif action == _State.PRESET_CREATE_VOICE_MENU:
        await _edit_message(query, "Voice settings:", _voice_menu_keyboard("preset_create:voice"))
    elif action == _State.PRESET_CREATE_VOICE:
        await _show_voice_selector(query, "preset_create", flow, back_data="preset_create:voice:back")
    elif action == _State.PRESET_CREATE_VOICE_TEXT:
        await _edit_message(query, "Voice-over text to speak (or /skip for none):")
    elif action == _State.PRESET_CREATE_VOICE_QUALITY:
        await _show_options(update, context, action, "Voice quality:", "voice_quality",
            *_FIELD_CHOICES["voice_quality"])
    elif action == _State.PRESET_CREATE_VOICE_SPEED:
        await _show_options(update, context, action, "Voice speed:", "voice_speed",
            *_FIELD_CHOICES["voice_speed"])
    elif action == _State.PRESET_CREATE_TTS_ENGINE:
        await _show_options(update, context, action, "TTS engine:", "tts_engine",
            *_FIELD_CHOICES["tts_engine"])
    elif action == _State.PRESET_CREATE_BANNER_MENU:
        await _edit_message(query, "Banner settings:", _banner_menu_keyboard("preset_create:banner"))
    elif action == _State.PRESET_CREATE_BANNER:
        await _edit_message(
            query,
            _BANNER_INSTRUCTIONS,
            _text_input_keyboard("preset_create:back", "preset_create:skip"),
        )
    elif action == _State.PRESET_CREATE_BANNER_POS:
        await _show_options(update, context, action, "Banner position:", "banner_position",
            *_FIELD_CHOICES["banner_position"])
    elif action == _State.PRESET_CREATE_BANNER_SCALE:
        await _show_options(update, context, action, "Banner scale:", "banner_scale",
            *_FIELD_CHOICES["banner_scale"])
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
    _State.PRESET_CREATE_VOICE_MENU: _State.PRESET_CREATE_CAPTION_POS,
    _State.PRESET_CREATE_VOICE: _State.PRESET_CREATE_VOICE_MENU,
    _State.PRESET_CREATE_VOICE_TEXT: _State.PRESET_CREATE_VOICE_MENU,
    _State.PRESET_CREATE_VOICE_QUALITY: _State.PRESET_CREATE_VOICE_MENU,
    _State.PRESET_CREATE_VOICE_SPEED: _State.PRESET_CREATE_VOICE_MENU,
    _State.PRESET_CREATE_TTS_ENGINE: _State.PRESET_CREATE_VOICE_MENU,
    _State.PRESET_CREATE_BANNER_MENU: _State.PRESET_CREATE_VOICE_MENU,
    _State.PRESET_CREATE_BANNER: _State.PRESET_CREATE_BANNER_MENU,
    _State.PRESET_CREATE_BANNER_POS: _State.PRESET_CREATE_BANNER_MENU,
    _State.PRESET_CREATE_BANNER_SCALE: _State.PRESET_CREATE_BANNER_MENU,
    _State.PRESET_CREATE_CHANNEL_BANNER: _State.PRESET_CREATE_BANNER_MENU,
}


async def _handle_preset_create_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: FlowState, query) -> None:
    data = query.data
    if data == "preset_create:skip":
        if flow.action == _State.PRESET_CREATE_VOICE:
            flow.data.pop("voice_over_voice", None)
            flow.action = _State.PRESET_CREATE_VOICE_MENU
            await _edit_message(query, "Voice settings:", _voice_menu_keyboard("preset_create:voice"))
        elif flow.action == _State.PRESET_CREATE_BANNER:
            flow.data.pop("banner_path", None)
            flow.data.pop("banner_url", None)
            flow.action = _State.PRESET_CREATE_BANNER_MENU
            await _edit_message(query, "Banner settings:", _banner_menu_keyboard("preset_create:banner"))
        return

    if data.startswith("preset_create:set:"):
        parts = data.split(":", 3)
        if len(parts) == 4:
            field_name, raw_value = parts[2], parts[3]
            flow.data[field_name] = _coerce_choice_value(field_name, raw_value)
            flow.action = _State.PRESET_CREATE_VOICE_MENU
            await _edit_message(query, "Voice settings:", _voice_menu_keyboard("preset_create:voice"))
        return

    if data == "preset_create:voice_over_voice_custom":
        flow.action = _State.PRESET_CREATE_VOICE
        await _edit_message(
            query,
            "Send a custom voice name.",
            _text_input_keyboard("preset_create:voice:back", "preset_create:skip"),
        )
        return

    if data in ("preset_create:back", "preset_create:cancel"):
        if data == "preset_create:cancel" or _CREATE_PREV_STEP.get(flow.action) is None:
            await _edit_message(query, "Cancelled preset creation.")
            flow.action = _State.MENU
            flow.data.clear()
            await _show_menu(update, context)
            return
        prev = _CREATE_PREV_STEP[flow.action]
        if flow.action == _State.PRESET_CREATE_BANNER and (
            flow.data.get("banner_path") or flow.data.get("banner_url")
        ):
            prev = _State.PRESET_CREATE_BANNER_MENU
        if flow.action == _State.PRESET_CREATE_CAPTION_STYLE:
            flow.data.pop("caption_style", None)
        elif flow.action == _State.PRESET_CREATE_CAPTION_POS:
            flow.data.pop("caption_position", None)
        elif flow.action in (_State.PRESET_CREATE_VOICE, _State.PRESET_CREATE_VOICE_TEXT,
                             _State.PRESET_CREATE_VOICE_QUALITY, _State.PRESET_CREATE_VOICE_SPEED,
                             _State.PRESET_CREATE_TTS_ENGINE):
            pass
        elif flow.action == _State.PRESET_CREATE_BANNER_POS:
            flow.data.pop("banner_position", None)
        elif flow.action == _State.PRESET_CREATE_BANNER_SCALE:
            flow.data.pop("banner_scale", None)
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

    if data == "preset_create:voice:done":
        flow.data.setdefault("voice_quality", "basic")
        flow.data.setdefault("voice_speed", 1.0)
        flow.data.setdefault("tts_engine", "auto")
        flow.action = _State.PRESET_CREATE_BANNER_MENU
        await _edit_message(query, "Banner settings:", _banner_menu_keyboard("preset_create:banner"))
        return

    if data == "preset_create:voice:back":
        flow.action = _State.PRESET_CREATE_VOICE_MENU
        await _edit_message(query, "Voice settings:", _voice_menu_keyboard("preset_create:voice"))
        return

    if data == "preset_create:banner:done":
        flow.data.setdefault("watermark_removal", True)
        flow.data.setdefault("watermark_position", "auto")
        flow.data.setdefault("channel_banner", False)
        flow.action = _State.PRESET_CREATE_CHANNEL_BANNER
        await _show_confirm(update, context, flow.action, "Channel banner for landscape videos?")
        return

    if data.startswith("preset_create:voice:"):
        inner = data.split(":", 2)[-1]
        if inner == "voice_over_voice":
            flow.action = _State.PRESET_CREATE_VOICE
            await _show_voice_selector(query, "preset_create", flow, back_data="preset_create:voice:back")
        elif inner == "voice_text":
            flow.action = _State.PRESET_CREATE_VOICE_TEXT
            await _edit_message(query, "Voice-over text to speak (or /skip for none):")
        elif inner == "voice_quality":
            flow.action = _State.PRESET_CREATE_VOICE_QUALITY
            await _show_options(update, context, flow.action, "Voice quality:", "voice_quality",
                *_FIELD_CHOICES["voice_quality"])
        elif inner == "voice_speed":
            flow.action = _State.PRESET_CREATE_VOICE_SPEED
            await _show_options(update, context, flow.action, "Voice speed:", "voice_speed",
                *_FIELD_CHOICES["voice_speed"])
        elif inner == "tts_engine":
            flow.action = _State.PRESET_CREATE_TTS_ENGINE
            await _show_options(update, context, flow.action, "TTS engine:", "tts_engine",
                *_FIELD_CHOICES["tts_engine"])
        return

    if data.startswith("preset_create:banner:"):
        inner = data.split(":", 2)[-1]
        if inner == "banner_path":
            flow.action = _State.PRESET_CREATE_BANNER
            await _edit_message(
                query,
                _BANNER_INSTRUCTIONS,
                _text_input_keyboard("preset_create:back", "preset_create:skip"),
            )
        elif inner == "banner_position":
            flow.action = _State.PRESET_CREATE_BANNER_POS
            await _show_options(update, context, flow.action, "Banner position:", "banner_position",
                *_FIELD_CHOICES["banner_position"])
        elif inner == "banner_scale":
            flow.action = _State.PRESET_CREATE_BANNER_SCALE
            await _show_options(update, context, flow.action, "Banner scale:", "banner_scale",
                *_FIELD_CHOICES["banner_scale"])
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
        flow.action = _State.PRESET_CREATE_VOICE_MENU
        await _edit_message(query, "Voice settings:", _voice_menu_keyboard("preset_create:voice"))
    elif field in ("voice_quality", "voice_speed", "tts_engine", "voice_text", "voice_over_voice"):
        flow.action = _State.PRESET_CREATE_VOICE_MENU
        await _edit_message(query, "Voice settings:", _voice_menu_keyboard("preset_create:voice"))
    elif field in ("banner_position", "banner_scale", "banner_path"):
        flow.action = _State.PRESET_CREATE_BANNER_MENU
        await _edit_message(query, "Banner settings:", _banner_menu_keyboard("preset_create:banner"))
    elif field == "channel_banner":
        flow.data["channel_banner"] = value == "yes"
        await _finalize_preset_create(update, context, context.application.bot_data["db_path"])


async def _handle_yesno(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: FlowState, query, next_action: str, choice: bool) -> None:
    if next_action == str(_State.PRESET_CREATE_CHANNEL_BANNER):
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
        await _edit_message(
            query,
            "Send the preset name (for example “default”).",
            _text_input_keyboard("settings:menu", "settings:menu"),
        )
        return

    if query.data == "settings:profile_banner":
        flow.action = "set_profile_banner"
        await _edit_message(
            query,
            "Send a photo to use as your profile banner.",
            _text_input_keyboard("settings:menu", "settings:menu"),
        )
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
        if field_name == "voice_menu":
            preset = await _get_preset(
                context.application.bot_data["db_path"],
                query.from_user.id,
                preset_id,
            )
            engine = (preset.tts_engine if preset else None) or "auto"
            rows = [
                [InlineKeyboardButton("🎤 Voice Name", callback_data=f"preset:field:{preset_id}:voice_over_voice")],
                [InlineKeyboardButton("✨ Voice Quality", callback_data=f"preset:field:{preset_id}:voice_quality")],
                [InlineKeyboardButton("⏩ Voice Speed", callback_data=f"preset:field:{preset_id}:voice_speed")],
                [InlineKeyboardButton("🔊 TTS Engine", callback_data=f"preset:field:{preset_id}:tts_engine")],
                [InlineKeyboardButton("✅ Done", callback_data=f"preset:menu:{preset_id}")],
            ]
            await _edit_message(
                query,
                _tts_engine_overview(engine),
                InlineKeyboardMarkup(rows),
            )
            return
        if field_name == "banner_menu":
            rows = [
                [InlineKeyboardButton("🖼️ Banner Image", callback_data=f"preset:field:{preset_id}:banner_path")],
                [InlineKeyboardButton("📌 Banner Position", callback_data=f"preset:field:{preset_id}:banner_position")],
                [InlineKeyboardButton("📏 Banner Scale", callback_data=f"preset:field:{preset_id}:banner_scale")],
                [InlineKeyboardButton("✅ Done", callback_data=f"preset:menu:{preset_id}")],
            ]
            await _edit_message(query, "Banner settings:", InlineKeyboardMarkup(rows))
            return
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

    if query.data.startswith("preset:skip:"):
        parts = query.data.split(":", 3)
        if len(parts) == 4:
            await _apply_preset_edit_callback(
                update,
                context,
                int(parts[2]),
                parts[3],
                None,
            )
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
    if flow is None or not update.message:
        return False
    if flow.action not in (_State.PRESET_CREATE_BANNER, "set_profile_banner", _State.PRESET_EDIT_FIELD):
        return False
    if (
        flow.action == _State.PRESET_EDIT_FIELD
        and flow.data.get("field_name") != "banner_path"
    ):
        return False

    message = update.message
    upload = message.photo[-1] if message.photo else message.document
    if upload is None:
        return False

    suffix = ".jpg"
    if message.document is not None:
        suffix = Path(message.document.file_name or "").suffix.lower()
        mime_type = (message.document.mime_type or "").lower()
        if suffix not in _BANNER_IMAGE_EXTENSIONS or not mime_type.startswith("image/"):
            await message.reply_text(
                "That file is not a supported banner image. Send a JPG, PNG, or WebP image."
            )
            return True
        if (
            message.document.file_size is not None
            and message.document.file_size > _MAX_BANNER_IMAGE_BYTES
        ):
            await message.reply_text("That banner is too large. Send an image under 20 MB.")
            return True

    user = update.effective_user
    if user is None:
        return False
    storage_dir: Path = context.application.bot_data.get("storage_dir", Path("runtime/jobs"))
    banners_dir = storage_dir / "banners"
    banners_dir.mkdir(parents=True, exist_ok=True)

    file = await upload.get_file()
    dest = banners_dir / f"banner_{user.id}{suffix}"
    await file.download_to_drive(dest)

    if flow.action == "set_profile_banner":
        flow.action = _State.MENU
        await message.reply_text("Profile banner saved!")
        await _show_menu(update, context)
        return True

    if flow.action == _State.PRESET_CREATE_BANNER:
        flow.data["banner_path"] = str(dest)
        flow.action = _State.PRESET_CREATE_BANNER_MENU
        await message.reply_text("Banner saved!", reply_markup=_banner_menu_keyboard("preset_create:banner"))
        return True

    if flow.action == _State.PRESET_EDIT_FIELD and flow.data.get("field_name") == "banner_path":
        preset_id = flow.data.get("preset_id")
        if preset_id is not None:
            await _apply_preset_edit(update, context, preset_id, "banner_path", str(dest))
        else:
            await message.reply_text("Could not determine which preset to update.")
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
        flow.data["caption_color"] = "white"
        flow.data["caption_style"] = "basic"
        flow.data["caption_position"] = "bottom"
        flow.data["voice_quality"] = "basic"
        flow.data["voice_speed"] = 1.0
        flow.data["tts_engine"] = "auto"
        flow.data["watermark_removal"] = True
        flow.data["watermark_position"] = "auto"
        flow.data["channel_banner"] = False
        flow.action = _State.PRESET_CREATE_CAPTION_COLOR
        await update.message.reply_text(
            "Caption colour:",
            reply_markup=_color_hue_keyboard("preset_create:cancel", "preset_create:hue"),
        )
        return True

    if flow.action == _State.PRESET_CREATE_VOICE:
        if text.lower() != "/skip":
            flow.data["voice_over_voice"] = text
        flow.action = _State.PRESET_CREATE_VOICE_MENU
        await _edit_or_send(update, "Voice settings:", _voice_menu_keyboard("preset_create:voice"))
        return True

    if flow.action == _State.PRESET_CREATE_VOICE_TEXT:
        if text.lower() != "/skip":
            flow.data["voice_text"] = text
        flow.action = _State.PRESET_CREATE_VOICE_MENU
        await _edit_or_send(update, "Voice settings:", _voice_menu_keyboard("preset_create:voice"))
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
        flow.action = _State.PRESET_CREATE_VOICE_MENU
        await _edit_or_send(update, "Voice settings:", _voice_menu_keyboard("preset_create:voice"))
        return True

    if flow.action == _State.PRESET_CREATE_BANNER:
        if text.lower() != "/skip":
            storage_dir: Path = context.application.bot_data.get("storage_dir", Path("runtime/jobs"))
            banner_file = storage_dir / "banners" / f"banner_{user.id if user else 0}.png"
            if text.lower() in ("profile", "p") and banner_file.is_file():
                flow.data["banner_path"] = str(banner_file)
            elif text.startswith("http://") or text.startswith("https://"):
                flow.data["banner_url"] = text
            else:
                await update.message.reply_text("Send a photo, type 'profile', paste an image URL, or /skip:")
                return True
        flow.action = _State.PRESET_CREATE_BANNER_MENU
        await _edit_or_send(update, "Banner settings:", _banner_menu_keyboard("preset_create:banner"))
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
            if field_name == "banner_path" and value is not None:
                if value.startswith("http"):
                    import urllib.request
                    storage_dir: Path = context.application.bot_data.get("storage_dir", Path("runtime/jobs"))
                    banners_dir = storage_dir / "banners"
                    banners_dir.mkdir(parents=True, exist_ok=True)
                    banner_dest = banners_dir / f"banner_{user.id}_{preset_id}.png"
                    try:
                        await update.message.reply_text("Downloading banner image...")
                        urllib.request.urlretrieve(value, banner_dest)
                        value = str(banner_dest)
                    except Exception as exc:
                        await update.message.reply_text(f"Failed to download banner: {exc}")
                        return True
                elif not Path(value).is_file():
                    await update.message.reply_text("Banner image file not found. Send a URL, a valid local path, or /skip to clear.")
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
    rows = _build_config_rows(
        values,
        field_prefix=f"preset:field:{preset.id}",
        toggle_prefix=f"preset:set:{preset.id}",
        include_watermark_position=True,
    )
    rows.append([InlineKeyboardButton(f"{active_label}", callback_data=f"preset:active:{preset.id}")])
    rows.append([InlineKeyboardButton("🔗 Share", callback_data=f"preset:share:{preset.id}")])
    rows.append([InlineKeyboardButton("🗑️ Delete", callback_data=f"preset:delete:{preset.id}")])
    rows.append([InlineKeyboardButton("← Back", callback_data="preset:back")])

    status = "⭐ Active " if is_active else ""
    text = (
        f"{status}Preset: {preset.name}\n"
        f"Tap any setting to change it."
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

    if field_name == "banner_path":
        await _edit_message(
            query,
            _BANNER_INSTRUCTIONS,
            _text_input_keyboard(back_data, f"preset:skip:{preset_id}:banner_path"),
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
        f"Send a new value for {pretty}, or tap Skip to clear it.",
        _text_input_keyboard(back_data, f"preset:skip:{preset_id}:{field_name}"),
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
        await _edit_or_send(update, "Downloading banner image...")
        import urllib.request
        storage_dir: Path = context.application.bot_data.get("storage_dir", Path("runtime/jobs"))
        banner_dest = storage_dir / f"banner_{user.id}_{flow.data['name']}.png"
        try:
            urllib.request.urlretrieve(banner_url, banner_dest)
            flow.data["banner_path"] = str(banner_dest)
        except Exception as exc:
            LOGGER.warning("Banner download failed: %s", exc)
            await _edit_or_send(update, f"Banner download failed: {exc}. Preset created without banner.")

    preset_values = dict(flow.data)
    preset_name = preset_values.pop("name")
    preset = await create_preset(db_path, user.id, preset_name, **preset_values)
    flow.action = _State.PRESET_LIST
    await _edit_or_send(update, f"Preset \"{preset.name}\" created.")
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
    file_size = await stage_edit_source(source_path, dest)
    await update_edit_job(db_path, edit.id, file_path=str(dest), file_size=file_size)
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
    file_size = await stage_edit_source(source_path, dest)
    await update_edit_job(db_path, edit.id, file_path=str(dest), file_size=file_size)
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
        return
    except Exception as text_error:
        try:
            await query.edit_message_caption(caption=text, reply_markup=reply_markup)
            return
        except Exception:
            LOGGER.warning("Could not edit menu message: %s", text_error)


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
    pool_item = await create_pool_item(
        db_path,
        user.id,
        edit.file_path,
        source_job_id=edit.source_job_id,
        edit_job_id=edit.id,
        file_size=edit.file_size,
        title=f"Edit #{edit.id}",
    )
    await update.callback_query.answer("Added to pool")
    await _edit_or_send(update, f"Edit job #{edit_id} added to pool as item #{pool_item.id}.", None)


def build_editconfig_keyboard(edit: EditJob, preset: Preset | None = None) -> InlineKeyboardMarkup:
    values = _effective_edit_snapshot(edit, preset)
    prefix = f"editcfg:{edit.id}"
    rows = _build_config_rows(
        values,
        field_prefix=prefix,
        toggle_prefix=f"{prefix}:set",
        include_watermark_position=True,
    )
    rows = [
        row for row in rows
        if not row[0].callback_data.endswith(":caption_text")
    ]
    menu = Menu()
    menu.rows.extend(rows)
    menu.action("💾 Save Config to Preset", f"{prefix}:save_preset")
    menu.action("🎬 Render now", f"{prefix}:render")
    menu.back(f"{prefix}:download", "← Back to Download")
    return menu.build()


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


async def handle_editconfig_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple | None:
    """Handle editcfg:* callbacks. Returns ('render', edit_id) when render was requested."""
    query = update.callback_query
    if query is None or query.data is None:
        return None
    await query.answer()
    db_path: Path = context.application.bot_data["db_path"]
    data: str = query.data

    if not data.startswith("editcfg:"):
        return None

    parts = data.split(":", 2)
    if len(parts) < 3:
        return None
    try:
        edit_id = int(parts[1])
    except ValueError:
        await _edit_message(query, "Invalid edit ID.")
        return None
    rest = parts[2]

    flow = context.user_data.get("settings_flow")
    if isinstance(flow, FlowState):
        pass
    elif not isinstance(flow, dict):
        flow = {}
        context.user_data["settings_flow"] = flow

    if isinstance(flow, dict):
        flow["action"] = "editconfig"
        flow["edit_id"] = edit_id

    prefix = f"editcfg:{edit_id}"

    if rest == "back":
        if isinstance(flow, dict) and flow.get("field_name"):
            flow.pop("field_name", None)
        await show_editconfig_menu(update, context, edit_id)
        return None

    if rest == "menu":
        if isinstance(flow, dict):
            flow.pop("field_name", None)
        await show_editconfig_menu(update, context, edit_id)
        return None

    if rest == "download":
        edit = await get_edit_job(db_path, edit_id)
        if edit is not None:
            return ("download", edit.source_job_id)
        return None

    if rest == "render":
        return ("render", edit_id)

    if rest.startswith("skip:"):
        field_name = rest.split(":", 1)[1]
        if field_name == "save_preset_name":
            if isinstance(flow, dict):
                flow.pop("field_name", None)
            await show_editconfig_menu(update, context, edit_id)
            return None
        await update_edit_job(db_path, edit_id, **{field_name: None})
        if isinstance(flow, dict):
            flow.pop("field_name", None)
        await show_editconfig_menu(update, context, edit_id)
        return None

    if rest == "save_preset":
        edit = await get_edit_job(db_path, edit_id)
        if edit is None:
            await _edit_message(query, "Edit job not found.")
            return None
        presets = await list_presets(db_path, edit.user_id)
        menu = Menu()
        for preset in presets:
            menu.action(
                f"💾 {preset.name}",
                f"{prefix}:save_preset:{preset.id}",
            )
        menu.action("➕ Create New Preset", f"{prefix}:save_preset:new")
        menu.back(f"{prefix}:menu")
        await _edit_message(
            query,
            "Save this edit configuration:\nChoose a preset to overwrite, or create a new one.",
            menu.build(),
        )
        return None

    if rest.startswith("save_preset:"):
        target = rest.split(":", 1)[1]
        edit = await get_edit_job(db_path, edit_id)
        if edit is None:
            await _edit_message(query, "Edit job not found.")
            return None
        if target == "new":
            if isinstance(flow, dict):
                flow["field_name"] = "save_preset_name"
            await _edit_message(
                query,
                "Send a name for the new preset.",
                _text_input_keyboard(
                    f"{prefix}:save_preset",
                    f"{prefix}:skip:save_preset_name",
                ),
            )
            return None
        try:
            preset_id = int(target)
        except ValueError:
            return None
        source_preset = (
            await _get_preset(db_path, edit.user_id, edit.preset_id)
            if edit.preset_id else None
        )
        updated = await update_preset(
            db_path,
            preset_id,
            edit.user_id,
            **_effective_edit_snapshot(edit, source_preset),
        )
        if updated is None:
            await query.answer("Preset not found", show_alert=True)
        else:
            await show_editconfig_menu(
                update,
                context,
                edit_id,
                intro=f"✅ Saved edit configuration to preset “{updated.name}”.",
            )
        return None

    if rest.startswith("hue:"):
        hue = rest.split(":", 1)[-1]
        await _edit_message(
            query,
            f"Pick a shade of {color_label(hue)}:",
            _color_shade_keyboard(hue, f"{prefix}:caption_color", f"{prefix}:set:caption_color"),
        )
        return None

    if rest.startswith("set:"):
        inner = rest.split(":", 2)
        if len(inner) < 3:
            return None
        field_name = inner[1]
        raw_value = inner[2]
        value = _coerce_choice_value(field_name, raw_value)
        await update_edit_job(db_path, edit_id, **{field_name: value})
        if isinstance(flow, dict):
            flow.pop("field_name", None)
        await show_editconfig_menu(update, context, edit_id)
        return None

    if rest == "voice_menu":
        if isinstance(flow, dict):
            flow["field_name"] = "voice_menu"
        rows = [
            [InlineKeyboardButton("🎤 Voice Name", callback_data=f"{prefix}:voice_menu:voice_over_voice")],
            [InlineKeyboardButton("✨ Voice Quality", callback_data=f"{prefix}:voice_menu:voice_quality")],
            [InlineKeyboardButton("⏩ Voice Speed", callback_data=f"{prefix}:voice_menu:voice_speed")],
            [InlineKeyboardButton("🔊 TTS Engine", callback_data=f"{prefix}:voice_menu:tts_engine")],
            [InlineKeyboardButton("✅ Done", callback_data=f"{prefix}:menu")],
        ]
        edit = await get_edit_job(db_path, edit_id)
        engine = (edit.tts_engine if edit else None) or "auto"
        await _edit_message(query, _tts_engine_overview(engine), InlineKeyboardMarkup(rows))
        return None

    if rest.startswith("voice_menu:"):
        field_name = rest.split(":", 1)[-1]
        if isinstance(flow, dict):
            flow["field_name"] = field_name
        if field_name == "voice_over_voice":
            await _show_voice_selector(query, prefix, flow, back_data=f"{prefix}:voice_menu")
            return None
        if field_name in _FIELD_CHOICES:
            pretty = field_name.replace("_", " ").title()
            await _edit_message(
                query,
                f"Choose {pretty}:",
                _choice_keyboard(field_name, f"{prefix}:voice_menu", f"{prefix}:set:{field_name}"),
            )
        else:
            pretty = field_name.replace("_", " ").title()
            await _edit_message(
                query,
                f"Send a new value for {pretty}, or tap Skip to clear it.",
                _text_input_keyboard(
                    f"{prefix}:voice_menu",
                    f"{prefix}:skip:{field_name}",
                ),
            )
        return None

    if rest == "banner_menu":
        if isinstance(flow, dict):
            flow["field_name"] = "banner_menu"
        rows = [
            [InlineKeyboardButton("🖼️ Banner Image", callback_data=f"{prefix}:banner_menu:banner_path")],
            [InlineKeyboardButton("📌 Banner Position", callback_data=f"{prefix}:banner_menu:banner_position")],
            [InlineKeyboardButton("📏 Banner Scale", callback_data=f"{prefix}:banner_menu:banner_scale")],
            [InlineKeyboardButton("✅ Done", callback_data=f"{prefix}:menu")],
        ]
        await _edit_message(query, "Banner settings:", InlineKeyboardMarkup(rows))
        return None

    if rest.startswith("banner_menu:"):
        field_name = rest.split(":", 1)[-1]
        if isinstance(flow, dict):
            flow["field_name"] = field_name
        if field_name == "banner_path":
            await _edit_message(
                query,
                _BANNER_INSTRUCTIONS,
                _text_input_keyboard(
                    f"{prefix}:banner_menu",
                    f"{prefix}:skip:banner_path",
                ),
            )
        elif field_name in _FIELD_CHOICES:
            pretty = field_name.replace("_", " ").title()
            await _edit_message(
                query,
                f"Choose {pretty}:",
                _choice_keyboard(field_name, f"{prefix}:banner_menu", f"{prefix}:set:{field_name}"),
            )
        else:
            pretty = field_name.replace("_", " ").title()
            await _edit_message(
                query,
                f"Send a new value for {pretty}, or tap Skip to clear it.",
                _text_input_keyboard(
                    f"{prefix}:banner_menu",
                    f"{prefix}:skip:{field_name}",
                ),
            )
        return None

    field = rest
    if isinstance(flow, dict):
        flow["field_name"] = field

    if field == "caption_color":
        await _edit_message(
            query,
            "Caption colour — pick a hue:",
            _color_hue_keyboard(f"{prefix}:menu", f"{prefix}:hue"),
        )
        return None

    if field == "voice_over_voice":
        await _show_voice_selector(query, prefix, flow, back_data=f"{prefix}:menu")
        return None

    if rest == "voice_over_voice_custom":
        if isinstance(flow, dict):
            flow["field_name"] = "voice_over_voice"
        await _edit_message(
            query,
            "Send a voice name (for example en-US-AriaNeural), or tap Skip to clear it.",
            _text_input_keyboard(
                f"{prefix}:voice_over_voice",
                f"{prefix}:skip:voice_over_voice",
            ),
        )
        return None

    if field in _FIELD_CHOICES:
        pretty = field.replace("_", " ").title()
        await _edit_message(
            query,
            f"Choose {pretty}:",
            _choice_keyboard(field, f"{prefix}:menu", f"{prefix}:set:{field}"),
        )
        return None

    pretty = field.replace("_", " ").title()
    await _edit_message(
        query,
        f"Send a new value for {pretty}, or tap Skip to clear it.",
        _text_input_keyboard(f"{prefix}:menu", f"{prefix}:skip:{field}"),
    )
    return None
