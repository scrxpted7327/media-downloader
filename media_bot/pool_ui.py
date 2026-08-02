from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from .access import ResourceNotFound, require_owned_job
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
    get_job,
    get_pool_item,
    get_workflow,
    list_classifications,
    list_pool_items,
    list_pool_tags,
    list_saved_edits_for_source,
    list_source_jobs_for_user,
    list_workflows,
    remove_pool_tag,
    update_workflow,
)

LOGGER = logging.getLogger(__name__)

_PAGE_SIZE = 6


async def _answer_callback(query, text: str | None = None, *, show_alert: bool = False) -> bool:
    """A stale Telegram callback must not prevent the requested pool action."""
    try:
        await query.answer(text, show_alert=show_alert)
        return True
    except BadRequest as exc:
        message = str(exc).lower()
        if "query is too old" in message or "query id is invalid" in message:
            LOGGER.info("Ignoring expired pool callback acknowledgement: %s", exc)
            return False
        raise


class _State:
    MENU = "pool_menu"
    POOL_LIST = "pool_list"
    POOL_ADD_PICK = "pool_add_pick"
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
    await _answer_callback(query)

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
        flow.action = _State.POOL_ADD_PICK
        flow.page = 0
        flow.data.clear()
        await _show_add_pick(update, context)
        return

    if query.data.startswith("pool:addpick:"):
        job_id = int(query.data.split(":")[-1])
        db_path: Path = context.application.bot_data["db_path"]
        job = await get_job(db_path, job_id)
        user = update.effective_user
        if job is None or job.file_path is None or user is None or job.user_id != user.id:
            await _answer_callback(query, "Source not found", show_alert=True)
            return
        flow.action = _State.POOL_ADD_NAME
        flow.data["source_job_id"] = job.id
        flow.data["file_path"] = job.file_path
        await _edit_message(query, "Send the title for this pool item (or /skip for none):")
        return

    if query.data == "pool:classify":
        flow.action = _State.POOL_LIST
        flow.page = 0
        await _edit_message(query, "Tap a pool item to manage its classifications.")
        await _show_pool_list(update, context)
        return

    if query.data == "pool:workflows":
        flow.action = _State.WORKFLOW_LIST
        flow.page = 0
        await _show_workflow_list(update, context)
        return

    if query.data == "pool:filter":
        flow.action = _State.FILTER
        flow.data.pop("filter_classification_id", None)
        await _show_filter(update, context)
        return

    if query.data.startswith("pool:filter:"):
        classification_id = int(query.data.split(":")[-1])
        flow.action = _State.POOL_LIST
        flow.page = 0
        flow.data["filter_classification_id"] = classification_id
        await _show_pool_list(update, context)
        return

    if query.data == "pool:filter_clear":
        flow.action = _State.POOL_LIST
        flow.page = 0
        flow.data.pop("filter_classification_id", None)
        await _show_pool_list(update, context)
        return

    if query.data.startswith("pool:page:"):
        flow.page = int(query.data.split(":")[-1])
        await _show_pool_list(update, context)
        return

    if query.data.startswith("pool:addpage:"):
        flow.page = int(query.data.split(":")[-1])
        await _show_add_pick(update, context)
        return

    if query.data.startswith("pool:item:"):
        pool_item_id = int(query.data.split(":")[-1])
        flow.data["pool_item_id"] = pool_item_id
        await _show_pool_item(update, context, pool_item_id)
        return

    if query.data.startswith("pool:send:"):
        pool_item_id = int(query.data.split(":")[-1])
        db_path: Path = context.application.bot_data["db_path"]
        item = await get_pool_item(db_path, pool_item_id)
        user = update.effective_user
        if item is None or user is None or item.user_id != user.id:
            await _answer_callback(query, "Pool item not found", show_alert=True)
            return
        path = Path(item.file_path)
        if not path.is_file():
            await _answer_callback(query, "Saved file is missing", show_alert=True)
            return
        await _answer_callback(query, "Sending…")
        with path.open("rb") as document:
            await query.message.reply_document(
                document=document,
                caption=item.title or f"Pool item #{item.id}",
            )
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
        item = await get_pool_item(db_path, pool_item_id)
        if user and item and item.user_id == user.id:
            tag = await add_pool_tag(db_path, pool_item_id, classification_id, user.id)
            if tag:
                await _answer_callback(query, "Tagged")
            else:
                await _answer_callback(query, "Already tagged", show_alert=True)
        else:
            await _answer_callback(query, "Unauthorized", show_alert=True)
        flow.action = _State.CLASSIFY_SELECT
        flow.data["pool_item_id"] = pool_item_id
        await _show_classify_select(update, context)
        return

    if query.data.startswith("pool:untag:"):
        parts = query.data.split(":")
        pool_item_id = int(parts[2])
        classification_id = int(parts[3])
        db_path: Path = context.application.bot_data["db_path"]
        user = update.effective_user
        item = await get_pool_item(db_path, pool_item_id)
        removed = bool(
            user
            and item
            and item.user_id == user.id
            and await remove_pool_tag(db_path, pool_item_id, classification_id)
        )
        await _answer_callback(query, "Removed" if removed else "Not found")
        flow.action = _State.CLASSIFY_SELECT
        flow.data["pool_item_id"] = pool_item_id
        await _show_classify_select(update, context)
        return

    if query.data.startswith("pool:delete:"):
        pool_item_id = int(query.data.split(":")[-1])
        db_path: Path = context.application.bot_data["db_path"]
        user = update.effective_user
        if user and await delete_pool_item(db_path, pool_item_id, user.id):
            await _answer_callback(query, "Deleted")
        else:
            await _answer_callback(query, "Failed", show_alert=True)
        flow.action = _State.POOL_LIST
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
        user = update.effective_user
        if wf and user and wf.user_id == user.id:
            await update_workflow(db_path, wf_id, user.id, enabled=not wf.enabled)
            await _answer_callback(query, "Updated")
        else:
            await _answer_callback(query, "Workflow not found", show_alert=True)
        await _show_workflow_list(update, context)
        return

    if query.data.startswith("workflow:delete:"):
        wf_id = int(query.data.split(":")[-1])
        db_path: Path = context.application.bot_data["db_path"]
        user = update.effective_user
        if user and await delete_workflow(db_path, wf_id, user.id):
            await _answer_callback(query, "Deleted")
        else:
            await _answer_callback(query, "Failed", show_alert=True)
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
        [InlineKeyboardButton("🏊 Pool", callback_data="pool:list")],
        [InlineKeyboardButton("➕ Add to Pool", callback_data="pool:add")],
        [InlineKeyboardButton("🏷️ Classifications", callback_data="pool:classify")],
        [InlineKeyboardButton("⚙️ Workflows", callback_data="pool:workflows")],
        [InlineKeyboardButton("🔎 Filter Pool", callback_data="pool:filter")],
    ])
    msg = "🏊 Pool menu:\nManage your video pool, classifications, and workflows."
    if update.message:
        await update.message.reply_text(msg, reply_markup=keyboard)
    elif update.callback_query:
        await _edit_message(update.callback_query, msg, keyboard)


async def _show_add_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow: FlowState = context.user_data["pool_flow"]
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
        rows.append([InlineKeyboardButton(label, callback_data=f"pool:addpick:{j.id}")])
    nav = []
    if flow.page > 0:
        nav.append(InlineKeyboardButton("← Back", callback_data=f"pool:addpage:{flow.page - 1}"))
    if start + _PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Forward →", callback_data=f"pool:addpage:{flow.page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 Menu", callback_data="pool:menu")])
    text = "Select a downloaded video to add to the pool:" if total else "No downloaded videos available."
    await _edit_or_send(update, text, InlineKeyboardMarkup(rows))


async def _show_pool_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow: FlowState = context.user_data["pool_flow"]
    db_path: Path = context.application.bot_data["db_path"]
    user = update.effective_user
    if user is None:
        return
    filter_id = flow.data.get("filter_classification_id")
    items = await list_pool_items(db_path, user.id, classification_id=filter_id, limit=50)
    total = len(items)
    start = flow.page * _PAGE_SIZE
    page = items[start:start + _PAGE_SIZE]

    rows = []
    for item in page:
        label = f"#{item.id} {item.title or item.file_path[-30:]}"
        rows.append([
            InlineKeyboardButton(label, callback_data=f"pool:item:{item.id}"),
            InlineKeyboardButton("🗑️ Delete", callback_data=f"pool:delete:{item.id}"),
        ])

    nav = []
    if flow.page > 0:
        nav.append(InlineKeyboardButton("← Back", callback_data=f"pool:page:{flow.page - 1}"))
    if start + _PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Forward →", callback_data=f"pool:page:{flow.page + 1}"))
    if nav:
        rows.append(nav)
    if filter_id is not None:
        rows.append([InlineKeyboardButton("✖ Clear filter", callback_data="pool:filter_clear")])
    rows.append([InlineKeyboardButton("🏠 Menu", callback_data="pool:menu")])

    if filter_id is not None:
        classification = await get_classification(db_path, filter_id)
        name = classification.name if classification else str(filter_id)
        text = f"🏊 Pool filtered by \"{name}\" ({total} items):"
    else:
        text = f"🏊 Pool ({total} items):"
    if total == 0:
        text = "Pool is empty. Use Add to Pool or /settings → Edit Existing Video."
    await _edit_or_send(update, text, InlineKeyboardMarkup(rows))


async def _show_pool_item(
    update: Update, context: ContextTypes.DEFAULT_TYPE, pool_item_id: int
) -> None:
    db_path: Path = context.application.bot_data["db_path"]
    user = update.effective_user
    item = await get_pool_item(db_path, pool_item_id)
    if item is None or user is None or item.user_id != user.id:
        await _edit_or_send(
            update,
            "Pool item not found.",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("← Back", callback_data="pool:list")
            ]]),
        )
        return

    related = []
    if item.source_job_id is not None:
        related = await list_saved_edits_for_source(
            db_path, user.id, item.source_job_id
        )

    rows = [[
        InlineKeyboardButton(
            "⬇️ Original" if item.edit_job_id is None else "⬇️ Download",
            callback_data=f"pool:send:{item.id}",
        )
    ]]
    for saved_edit in related:
        if saved_edit.id == item.id:
            continue
        rows.append([InlineKeyboardButton(
            f"⬇️ {saved_edit.title or f'Edit #{saved_edit.edit_job_id}'}",
            callback_data=f"pool:send:{saved_edit.id}",
        )])
    rows.append([
        InlineKeyboardButton(
            "🏷️ Classifications", callback_data=f"pool:classify:{item.id}"
        ),
        InlineKeyboardButton("🗑️ Remove", callback_data=f"pool:delete:{item.id}"),
    ])
    rows.append([InlineKeyboardButton("← Back", callback_data="pool:list")])
    count = len([related_item for related_item in related if related_item.id != item.id])
    await _edit_or_send(
        update,
        f"🏊 {item.title or f'Pool item #{item.id}'}\n"
        f"Saved edits included: {count}",
        InlineKeyboardMarkup(rows),
    )


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
    if pool_item_id is None:
        await _edit_or_send(update, "No pool item selected.", InlineKeyboardMarkup([
            [InlineKeyboardButton("← Back", callback_data="pool:list")],
        ]))
        return

    user = update.effective_user
    item = await get_pool_item(db_path, pool_item_id)
    if item is None or user is None or item.user_id != user.id:
        await _edit_or_send(update, "Pool item not found.", InlineKeyboardMarkup([
            [InlineKeyboardButton("← Back", callback_data="pool:list")],
        ]))
        return

    tags = await list_pool_tags(db_path, pool_item_id)
    tagged_ids = {t.classification_id for t in tags}
    classifications = await list_classifications(db_path)
    rows = []
    for c in classifications:
        if c.id in tagged_ids:
            rows.append([InlineKeyboardButton(
                f"✅ {c.name} (untag)",
                callback_data=f"pool:untag:{pool_item_id}:{c.id}",
            )])
        else:
            rows.append([InlineKeyboardButton(
                f"🏷️ {c.name}",
                callback_data=f"pool:tag:{pool_item_id}:{c.id}",
            )])
    rows.append([
        InlineKeyboardButton("🗑️ Delete item", callback_data=f"pool:delete:{pool_item_id}"),
        InlineKeyboardButton("← Back", callback_data="pool:list"),
    ])
    await _edit_or_send(
        update,
        f"Classifications for pool item #{pool_item_id}:",
        InlineKeyboardMarkup(rows),
    )


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
        rows.append([
            InlineKeyboardButton(
                f"{wf.name} [{status}] - {wf.action_type}",
                callback_data=f"workflow:toggle:{wf.id}",
            ),
            InlineKeyboardButton("🗑️", callback_data=f"workflow:delete:{wf.id}"),
        ])
    rows.append([InlineKeyboardButton("➕ Create workflow", callback_data="workflow:create")])
    rows.append([InlineKeyboardButton("🏠 Menu", callback_data="pool:menu")])
    text = f"⚙️ Workflows ({len(workflows)}):"
    await _edit_or_send(update, text, InlineKeyboardMarkup(rows))


async def _show_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db_path: Path = context.application.bot_data["db_path"]
    classifications = await list_classifications(db_path)
    rows = []
    for c in classifications:
        rows.append([InlineKeyboardButton(f"🏷️ {c.name}", callback_data=f"pool:filter:{c.id}")])
    rows.append([InlineKeyboardButton("🏠 Menu", callback_data="pool:menu")])
    text = "🔎 Filter pool by classification:" if classifications else "No classifications yet."
    await _edit_or_send(update, text, InlineKeyboardMarkup(rows))


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

    try:
        source = await require_owned_job(db_path, source_job_id, user.id)
    except ResourceNotFound:
        await update.message.reply_text("Not found or not authorized.")
        return
    if source.file_path is None:
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
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def _edit_or_send(update: Update, text: str, reply_markup=None) -> None:
    if update.callback_query:
        await _edit_message(update.callback_query, text, reply_markup)
    elif update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
