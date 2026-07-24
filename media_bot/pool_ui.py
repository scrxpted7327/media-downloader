from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .storage import (
    Classification,
    PoolItem,
    Workflow,
    add_pool_tag,
    create_classification,
    create_pool_item,
    create_workflow,
    delete_pool_item,
    delete_workflow,
    get_classification,
    get_pool_item,
    get_workflow,
    list_classifications,
    list_pool_items,
    list_workflows,
    remove_pool_tag,
    update_workflow,
)

LOGGER = logging.getLogger(__name__)

_PAGE_SIZE = 6


class _State:
    MENU = "pool_menu"
    POOL_LIST = "pool_list"
    POOL_ADD_NAME = "pool_add_name"
    CLASSIFY = "classify"
    CLASSIFY_SELECT = "classify_select"
    WORKFLOW_LIST = "workflow_list"
    WORKFLOW_CREATE_NAME = "workflow_create_name"
    WORKFLOW_CREATE_TRIGGER = "workflow_create_trigger"
    WORKFLOW_CREATE_ACTION = "workflow_create_action"
    FILTER = "filter"


@dataclass
class FlowState:
    action: str
    page: int = 0
    data: dict[str, Any] = field(default_factory=dict)


async def pool_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["pool_flow"] = FlowState(action=_State.MENU)
    await _show_menu(update, context)


async def pool_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()

    flow: FlowState = context.user_data.get("pool_flow")
    if flow is None:
        flow = FlowState(action=_State.MENU)
        context.user_data["pool_flow"] = flow

    if query.data == "pool:menu":
        flow.action = _State.MENU
        flow.data.clear()
        await _show_menu(update, context)
        return

    if query.data == "pool:list":
        flow.action = _State.POOL_LIST
        flow.page = 0
        await _show_pool_list(update, context)
        return

    if query.data == "pool:add":
        flow.action = _State.POOL_ADD_NAME
        flow.data.clear()
        await _edit_message(query, "Send the title for this pool item (or /skip for none):")
        return

    if query.data == "pool:classify":
        flow.action = _State.CLASSIFY
        await _show_classify_select(update, context)
        return

    if query.data == "pool:workflows":
        flow.action = _State.WORKFLOW_LIST
        flow.page = 0
        await _show_workflow_list(update, context)
        return

    if query.data == "pool:filter":
        flow.action = _State.FILTER
        await _show_filter(update, context)
        return

    if query.data.startswith("pool:page:"):
        flow.page = int(query.data.split(":")[-1])
        await _show_pool_list(update, context)
        return

    if query.data.startswith("pool:classify:"):
        pool_item_id = int(query.data.split(":")[-1])
        flow.action = _State.CLASSIFY_SELECT
        flow.data["pool_item_id"] = pool_item_id
        await _show_classify_select(update, context)
        return

    if query.data.startswith("pool:tag:"):
        parts = query.data.split(":")
        pool_item_id = int(parts[2])
        classification_id = int(parts[3])
        db_path: Path = context.application.bot_data["db_path"]
        user = update.effective_user
        if user:
            tag = await add_pool_tag(db_path, pool_item_id, classification_id, user.id)
            if tag:
                await query.answer("Tagged")
            else:
                await query.answer("Already tagged", show_alert=True)
        else:
            await query.answer("Unauthorized", show_alert=True)
        await _show_pool_list(update, context)
        return

    if query.data.startswith("pool:untag:"):
        parts = query.data.split(":")
        pool_item_id = int(parts[2])
        classification_id = int(parts[3])
        db_path: Path = context.application.bot_data["db_path"]
        removed = await remove_pool_tag(db_path, pool_item_id, classification_id)
        await query.answer("Removed" if removed else "Not found")
        await _show_pool_list(update, context)
        return

    if query.data.startswith("pool:delete:"):
        pool_item_id = int(query.data.split(":")[-1])
        db_path: Path = context.application.bot_data["db_path"]
        user = update.effective_user
        if user and await delete_pool_item(db_path, pool_item_id, user.id):
            await query.answer("Deleted")
        else:
            await query.answer("Failed", show_alert=True)
        await _show_pool_list(update, context)
        return

    if query.data == "workflow:create":
        flow.action = _State.WORKFLOW_CREATE_NAME
        flow.data.clear()
        await _edit_message(query, "Send workflow name:")
        return

    if query.data.startswith("workflow:toggle:"):
        wf_id = int(query.data.split(":")[-1])
        db_path: Path = context.application.bot_data["db_path"]
        wf = await get_workflow(db_path, wf_id)
        if wf:
            await update_workflow(db_path, wf_id, wf.user_id, enabled=not wf.enabled)
            await query.answer("Updated")
        await _show_workflow_list(update, context)
        return

    if query.data.startswith("workflow:delete:"):
        wf_id = int(query.data.split(":")[-1])
        db_path: Path = context.application.bot_data["db_path"]
        user = update.effective_user
        if user and await delete_workflow(db_path, wf_id, user.id):
            await query.answer("Deleted")
        else:
            await query.answer("Failed", show_alert=True)
        await _show_workflow_list(update, context)
        return

    if query.data == "workflow:back":
        flow.action = _State.WORKFLOW_LIST
        await _show_workflow_list(update, context)
        return


async def pool_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    flow: FlowState = context.user_data.get("pool_flow")
    if flow is None or not update.message or not update.message.text:
        return False

    text = update.message.text.strip()
    db_path: Path = context.application.bot_data["db_path"]

    if flow.action == _State.POOL_ADD_NAME:
        flow.data["title"] = text if text.lower() != "/skip" else None
        await _add_pool_from_job(update, context, db_path)
        return True

    if flow.action == _State.WORKFLOW_CREATE_NAME:
        flow.data["name"] = text
        flow.action = _State.WORKFLOW_CREATE_TRIGGER
        await update.message.reply_text("Send trigger classification name (or /skip for any):")
        return True

    if flow.action == _State.WORKFLOW_CREATE_TRIGGER:
        if text.lower() != "/skip":
            classification = await create_classification(db_path, name=text)
            flow.data["trigger_classification_id"] = classification.id
        else:
            flow.data["trigger_classification_id"] = None
        flow.action = _State.WORKFLOW_CREATE_ACTION
        await update.message.reply_text("Send action type (e.g. caption, voice_over, render):")
        return True

    if flow.action == _State.WORKFLOW_CREATE_ACTION:
        flow.data["action_type"] = text
        await _finalize_workflow_create(update, context, db_path)
        return True

    return False


async def _show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Pool", callback_data="pool:list")],
        [InlineKeyboardButton("Add to Pool", callback_data="pool:add")],
        [InlineKeyboardButton("Classifications", callback_data="pool:classify")],
        [InlineKeyboardButton("Workflows", callback_data="pool:workflows")],
        [InlineKeyboardButton("Filter Pool", callback_data="pool:filter")],
    ])
    msg = "Pool menu:\nManage your video pool, classifications, and workflows."
    if update.message:
        await update.message.reply_text(msg, reply_markup=keyboard)
    elif update.callback_query:
        await _edit_message(update.callback_query, msg, keyboard)


async def _show_pool_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow: FlowState = context.user_data["pool_flow"]
    db_path: Path = context.application.bot_data["db_path"]
    user = update.effective_user
    if user is None:
        return
    items = await list_pool_items(db_path, user.id, limit=50)
    total = len(items)
    start = flow.page * _PAGE_SIZE
    page = items[start:start + _PAGE_SIZE]

    rows = []
    for item in page:
        label = f"#{item.id} {item.title or item.file_path[-30:]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"pool:classify:{item.id}")])

    nav = []
    if flow.page > 0:
        nav.append(InlineKeyboardButton("← Back", callback_data=f"pool:page:{flow.page - 1}"))
    if start + _PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Forward →", callback_data=f"pool:page:{flow.page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("Menu", callback_data="pool:menu")])

    text = f"Pool ({total} items):"
    if total == 0:
        text = "Pool is empty. Add videos from /settings -> Edit Existing Video or send a video file."
    await _edit_or_send(update, text, InlineKeyboardMarkup(rows))


async def _show_classify_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow: FlowState = context.user_data["pool_flow"]
    db_path: Path = context.application.bot_data["db_path"]
    pool_item_id = flow.data.get("pool_item_id")
    if pool_item_id is None and update.callback_query and update.callback_query.data:
        try:
            pool_item_id = int(update.callback_query.data.split(":")[-1])
            flow.data["pool_item_id"] = pool_item_id
        except (ValueError, IndexError):
            pass

    classifications = await list_classifications(db_path)
    rows = []
    for c in classifications:
        rows.append([InlineKeyboardButton(
            c.name,
            callback_data=f"pool:tag:{pool_item_id}:{c.id}",
        )])
    rows.append([InlineKeyboardButton("← Back", callback_data="pool:menu")])
    await _edit_or_send(update, "Select a classification to tag this item:", InlineKeyboardMarkup(rows))


async def _show_workflow_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow: FlowState = context.user_data["pool_flow"]
    db_path: Path = context.application.bot_data["db_path"]
    user = update.effective_user
    if user is None:
        return
    workflows = await list_workflows(db_path, user.id)
    rows = []
    for wf in workflows:
        status = "ON" if wf.enabled else "OFF"
        rows.append([InlineKeyboardButton(
            f"{wf.name} [{status}] - {wf.action_type}",
            callback_data=f"workflow:toggle:{wf.id}",
        )])
    rows.append([InlineKeyboardButton("Create workflow", callback_data="workflow:create")])
    rows.append([InlineKeyboardButton("Menu", callback_data="pool:menu")])
    text = f"Workflows ({len(workflows)}):"
    await _edit_or_send(update, text, InlineKeyboardMarkup(rows))


async def _show_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db_path: Path = context.application.bot_data["db_path"]
    classifications = await list_classifications(db_path)
    rows = []
    for c in classifications:
        rows.append([InlineKeyboardButton(c.name, callback_data=f"pool:filter:{c.id}")])
    rows.append([InlineKeyboardButton("Menu", callback_data="pool:menu")])
    await _edit_or_send(update, "Filter pool by classification:", InlineKeyboardMarkup(rows))


async def _add_pool_from_job(update: Update, context: ContextTypes.DEFAULT_TYPE, db_path: Path) -> None:
    user = update.effective_user
    if user is None:
        return
    flow: FlowState = context.user_data["pool_flow"]
    source_job_id = flow.data.get("source_job_id")
    file_path = flow.data.get("file_path")
    title = flow.data.get("title")

    if source_job_id is None or file_path is None:
        await update.message.reply_text("No active edit job. Use /settings -> Edit Existing Video first.")
        return

    source = await get_job(db_path, source_job_id)
    if source is None or source.file_path is None:
        await update.message.reply_text("Source not found.")
        return

    pool_item = await create_pool_item(db_path, user.id, source.file_path, source_job_id=source_job_id, title=title)
    await update.message.reply_text(f"Added to pool as item #{pool_item.id}.")
    flow.action = _State.MENU
    await _show_menu(update, context)


async def _finalize_workflow_create(update: Update, context: ContextTypes.DEFAULT_TYPE, db_path: Path) -> None:
    user = update.effective_user
    if user is None:
        return
    flow: FlowState = context.user_data["pool_flow"]
    wf = await create_workflow(
        db_path,
        user.id,
        flow.data["name"],
        flow.data["action_type"],
        trigger_classification_id=flow.data.get("trigger_classification_id"),
    )
    await update.message.reply_text(f"Workflow \"{wf.name}\" created.")
    flow.action = _State.WORKFLOW_LIST
    await _show_workflow_list(update, context)


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
