from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


class Menu:
    """Small builder for consistent Telegram menus and navigation."""

    def __init__(self) -> None:
        self.rows: list[list[InlineKeyboardButton]] = []

    def action(self, label: str, callback_data: str) -> Menu:
        self.rows.append([InlineKeyboardButton(label, callback_data=callback_data)])
        return self

    def actions(self, *items: tuple[str, str]) -> Menu:
        self.rows.append([
            InlineKeyboardButton(label, callback_data=callback_data)
            for label, callback_data in items
        ])
        return self

    def toggle(self, label: str, enabled: bool, callback_data: str) -> Menu:
        icon = "✅" if enabled else "❌"
        return self.action(f"{icon} {label}", callback_data)

    def back(self, callback_data: str, label: str = "← Back") -> Menu:
        return self.action(label, callback_data)

    def home(self, callback_data: str, label: str = "🏠 Home") -> Menu:
        return self.action(label, callback_data)

    def build(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(self.rows)
