import asyncio
import logging
import os
import random
import sys
from dataclasses import dataclass
from typing import Optional, List

import dotenv
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from telethon import TelegramClient, types, errors
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest


# -------------------------------
# Logging
# -------------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.WARNING,
)
logger = logging.getLogger("bottito")


# -------------------------------
# Env
# -------------------------------
dotenv.load_dotenv()

DEFAULT_API_ID = os.environ.get("TG_API_ID", "")
DEFAULT_API_HASH = os.environ.get("TG_API_HASH", "")
SESSION_NAME = "telegram_session"

DEFAULT_MESSAGE = """New meme coin on Solana join https://t.me/+-iZU3WiJ7JQwYjg0 Free Airdrop available :-) type Airdrop for more info"""


@dataclass
class GroupInfo:
    row_id: int
    title: str
    group_type: str
    peer_id: int
    entity_id: int
    username: Optional[str]
    megagroup: Optional[bool]
    members_count: Optional[int]


def is_group_or_supergroup(entity) -> bool:
    if isinstance(entity, types.Chat):
        return True

    if isinstance(entity, types.Channel):
        return bool(getattr(entity, "megagroup", False))

    return False


async def get_group_members_count(client: TelegramClient, entity) -> Optional[int]:
    if not is_group_or_supergroup(entity):
        return None

    if isinstance(entity, types.Chat):
        count = getattr(entity, "participants_count", None)
        if count is not None:
            return int(count)

        full = await client(GetFullChatRequest(entity.id))
        participants = getattr(full.full_chat, "participants", None)

        if participants and getattr(participants, "participants", None):
            return len(participants.participants)

        return None

    if isinstance(entity, types.Channel) and getattr(entity, "megagroup", False):
        full = await client(GetFullChannelRequest(entity))
        count = getattr(full.full_chat, "participants_count", None)
        return int(count) if count is not None else None

    return None


class TelegramWorker(QThread):
    log = pyqtSignal(str)
    group_found = pyqtSignal(object)
    load_finished = pyqtSignal(int)
    send_progress = pyqtSignal(int, str)
    send_finished = pyqtSignal(int, int, int)
    error = pyqtSignal(str)

    def __init__(
        self,
        mode: str,
        api_id: int,
        api_hash: str,
        message: str = "",
        selected_peer_ids: Optional[List[int]] = None,
        dry_run: bool = True,
        min_delay: int = 30,
        max_delay: int = 90,
        min_members: int = 0,
        parent=None,
    ):
        super().__init__(parent)
        self.mode = mode
        self.api_id = api_id
        self.api_hash = api_hash
        self.message = message
        self.selected_peer_ids = selected_peer_ids or []
        self.dry_run = dry_run
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.min_members = min_members
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True

    def run(self):
        try:
            asyncio.run(self._run_async())
        except Exception as e:
            self.error.emit(f"{type(e).__name__}: {e}")

    async def _run_async(self):
        client = TelegramClient(SESSION_NAME, self.api_id, self.api_hash)

        try:
            await client.start()
            self.log.emit("Logged in.")

            if self.mode == "load":
                await self._load_groups(client)

            elif self.mode == "send":
                await self._send_to_selected_groups(client)

        finally:
            await client.disconnect()
            self.log.emit("Disconnected.")

    @staticmethod
    async def collect_all_dialogs(client: TelegramClient):
        """
        Collect dialogs from:
          - all folders
          - non-archived
          - archived

        De-duplicates by dialog.id.
        """
        dialogs_by_id = {}

        scan_modes = [
            ("all", {}),
            ("main", {"archived": False}),
            ("archived", {"archived": True}),
            ("folder_0", {"folder": 0}),
            ("folder_1", {"folder": 1}),
        ]

        for label, kwargs in scan_modes:
            try:
                async for dialog in client.iter_dialogs(limit=None, **kwargs):
                    dialogs_by_id[int(dialog.id)] = dialog
            except Exception as e:
                logger.warning(
                    "Failed scanning dialogs mode=%s error=%s: %s",
                    label,
                    type(e).__name__,
                    e,
                )

        return list(dialogs_by_id.values())

    async def _load_groups(self, client: TelegramClient):
        count_groups = 0
        count_all = 0
        count_users = 0
        count_normal_groups = 0
        count_supergroups = 0

        count_broadcast_channels = 0
        count_other_channels = 0
        count_other = 0

        self.log.emit("Scanning all Telegram dialogs: all + main + archived...")

        dialogs = await self.collect_all_dialogs(client)

        self.log.emit(f"Raw unique dialogs found: {len(dialogs)}")

        for dialog in dialogs:
            if self._stop_requested:
                self.log.emit("Loading stopped.")
                break

            count_all += 1
            entity = dialog.entity

            if isinstance(entity, types.User):
                count_users += 1
                continue

            if isinstance(entity, types.Chat):
                count_normal_groups += 1

            elif isinstance(entity, types.Channel):
                if getattr(entity, "megagroup", False):
                    count_supergroups += 1
                elif getattr(entity, "broadcast", False):
                    count_broadcast_channels += 1
                    continue
                else:
                    count_other_channels += 1
                    continue
            else:
                count_other += 1
                continue

            if not is_group_or_supergroup(entity):
                continue

            is_supergroup = isinstance(entity, types.Channel) and getattr(entity, "megagroup", False)
            group_type = "supergroup" if is_supergroup else "group"

            members_count = None
            try:
                members_count = await get_group_members_count(client, entity)
            except Exception as e:
                self.log.emit(
                    f"Could not read members for {dialog.name}: "
                    f"{type(e).__name__}: {e}"
                )

            group = GroupInfo(
                row_id=count_groups,
                title=dialog.name or "",
                group_type=group_type,
                peer_id=int(dialog.id),
                entity_id=int(getattr(entity, "id", 0)),
                username=getattr(entity, "username", None),
                megagroup=getattr(entity, "megagroup", None),
                members_count=members_count,
            )

            self.group_found.emit(group)
            count_groups += 1

        self.log.emit("Scan finished.")
        self.log.emit(f"All unique dialogs: {count_all}")
        self.log.emit(f"Users/private chats skipped: {count_users}")
        self.log.emit(f"Normal groups: {count_normal_groups}")
        self.log.emit(f"Supergroups: {count_supergroups}")
        self.log.emit(f"Broadcast channels skipped: {count_broadcast_channels}")
        self.log.emit(f"Other channels skipped: {count_other_channels}")
        self.log.emit(f"Other skipped: {count_other}")
        self.log.emit(f"Loaded groups/supergroups: {count_groups}")

        self.load_finished.emit(count_groups)

    async def _send_to_selected_groups(self, client: TelegramClient):
        sent = 0
        attempted = 0
        total_members = 0

        if not self.selected_peer_ids:
            self.log.emit("No groups selected.")
            self.send_finished.emit(0, 0, 0)
            return

        selected_set = set(int(x) for x in self.selected_peer_ids)

        async for dialog in client.iter_dialogs():
            if self._stop_requested:
                self.log.emit("Sending stopped.")
                break

            if int(dialog.id) not in selected_set:
                continue

            entity = dialog.entity

            if not is_group_or_supergroup(entity):
                self.log.emit(f"Skipping non-group: {dialog.name}")
                continue

            members_count = None
            try:
                members_count = await get_group_members_count(client, entity)
            except Exception as e:
                self.log.emit(f"Could not read members for {dialog.name}: {type(e).__name__}: {e}")

            if members_count is not None and members_count < self.min_members:
                self.log.emit(
                    f"Skipping {dialog.name}: members={members_count}, required={self.min_members}"
                )
                self.send_progress.emit(int(dialog.id), "Skipped: too small")
                continue

            attempted += 1
            title = dialog.name or str(dialog.id)

            if self.dry_run:
                self.log.emit(f"[DRY RUN] Would send to {title}, members={members_count}")
                self.send_progress.emit(int(dialog.id), "Dry run")
            else:
                try:
                    await client.send_message(
                        dialog.id,
                        self.message,
                        link_preview=False,
                    )

                    sent += 1
                    if members_count is not None:
                        total_members += members_count

                    self.log.emit(f"Sent to {title}, members={members_count}")
                    self.send_progress.emit(int(dialog.id), "Sent")

                except errors.FloodWaitError as e:
                    self.log.emit(f"FloodWait for {e.seconds}s on {title}")
                    self.send_progress.emit(int(dialog.id), f"FloodWait {e.seconds}s")
                    await asyncio.sleep(e.seconds + 5)

                except errors.ChatWriteForbiddenError:
                    self.log.emit(f"Cannot write to {title}")
                    self.send_progress.emit(int(dialog.id), "No write permission")

                except errors.UserBannedInChannelError:
                    self.log.emit(f"Banned/restricted in {title}")
                    self.send_progress.emit(int(dialog.id), "Restricted")

                except Exception as e:
                    self.log.emit(f"Failed {title}: {type(e).__name__}: {e}")
                    self.send_progress.emit(int(dialog.id), "Failed")

            if self._stop_requested:
                break

            delay = random.randint(self.min_delay, self.max_delay)
            self.log.emit(f"Sleeping {delay}s...")
            await asyncio.sleep(delay)

        self.send_finished.emit(sent, attempted, total_members)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Telegram Group Messenger - PyQt5")
        self.resize(1200, 750)

        self.worker: Optional[TelegramWorker] = None
        self.peer_id_to_row = {}

        self.api_id_input = QLineEdit(DEFAULT_API_ID)
        self.api_hash_input = QLineEdit(DEFAULT_API_HASH)
        self.api_hash_input.setEchoMode(QLineEdit.Password)

        self.message_edit = QTextEdit()
        self.message_edit.setPlainText(DEFAULT_MESSAGE)

        self.min_delay_spin = QSpinBox()
        self.min_delay_spin.setRange(1, 3600)
        self.min_delay_spin.setValue(30)

        self.max_delay_spin = QSpinBox()
        self.max_delay_spin.setRange(1, 3600)
        self.max_delay_spin.setValue(90)

        self.min_members_spin = QSpinBox()
        self.min_members_spin.setRange(0, 10_000_000)
        self.min_members_spin.setValue(0)

        self.dry_run_checkbox = QCheckBox("Dry run - do not send")
        self.dry_run_checkbox.setChecked(True)

        self.load_button = QPushButton("Load Groups")
        self.send_button = QPushButton("Send to Selected")
        self.stop_button = QPushButton("Stop")
        self.select_all_button = QPushButton("Select All")
        self.unselect_all_button = QPushButton("Unselect All")

        self.stop_button.setEnabled(False)
        self.send_button.setEnabled(False)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "Send",
            "Title",
            "Type",
            "Peer ID",
            "Username",
            "Members",
            "Megagroup",
            "Status",
        ])
        self.table.horizontalHeader().setStretchLastSection(True)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)

        self.status_label = QLabel("Ready.")

        self._build_layout()
        self._connect_events()

    def _build_layout(self):
        root = QWidget()
        root_layout = QVBoxLayout(root)

        settings_group = QGroupBox("Telegram Settings")
        settings_layout = QFormLayout(settings_group)
        settings_layout.addRow("TG_API_ID:", self.api_id_input)
        settings_layout.addRow("TG_API_HASH:", self.api_hash_input)

        options_group = QGroupBox("Send Options")
        options_layout = QFormLayout(options_group)
        options_layout.addRow("Min delay seconds:", self.min_delay_spin)
        options_layout.addRow("Max delay seconds:", self.max_delay_spin)
        options_layout.addRow("Min members:", self.min_members_spin)
        options_layout.addRow("", self.dry_run_checkbox)

        message_group = QGroupBox("Message")
        message_layout = QVBoxLayout(message_group)
        message_layout.addWidget(self.message_edit)

        top_layout = QHBoxLayout()
        top_layout.addWidget(settings_group, 1)
        top_layout.addWidget(options_group, 1)
        top_layout.addWidget(message_group, 2)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self.load_button)
        button_layout.addWidget(self.select_all_button)
        button_layout.addWidget(self.unselect_all_button)
        button_layout.addWidget(self.send_button)
        button_layout.addWidget(self.stop_button)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self.log_edit)
        splitter.setSizes([500, 200])

        root_layout.addLayout(top_layout)
        root_layout.addLayout(button_layout)
        root_layout.addWidget(splitter)
        root_layout.addWidget(self.status_label)

        self.setCentralWidget(root)

    def _connect_events(self):
        self.load_button.clicked.connect(self.load_groups)
        self.send_button.clicked.connect(self.send_selected)
        self.stop_button.clicked.connect(self.stop_worker)
        self.select_all_button.clicked.connect(lambda: self.set_all_selected(True))
        self.unselect_all_button.clicked.connect(lambda: self.set_all_selected(False))

    def parse_api_settings(self):
        api_id_text = self.api_id_input.text().strip()
        api_hash = self.api_hash_input.text().strip()

        if not api_id_text:
            raise ValueError("TG_API_ID is empty.")

        if not api_hash:
            raise ValueError("TG_API_HASH is empty.")

        return int(api_id_text), api_hash

    def append_log(self, text: str):
        self.log_edit.append(text)

    def set_busy(self, busy: bool):
        self.load_button.setEnabled(not busy)
        self.send_button.setEnabled(not busy and self.table.rowCount() > 0)
        self.stop_button.setEnabled(busy)

    def clear_table(self):
        self.table.setRowCount(0)
        self.peer_id_to_row.clear()

    def load_groups(self):
        try:
            api_id, api_hash = self.parse_api_settings()
        except Exception as e:
            QMessageBox.warning(self, "Invalid settings", str(e))
            return

        self.clear_table()
        self.append_log("Loading groups/supergroups...")
        self.status_label.setText("Loading groups...")

        self.worker = TelegramWorker(
            mode="load",
            api_id=api_id,
            api_hash=api_hash,
        )
        self.worker.log.connect(self.append_log)
        self.worker.group_found.connect(self.add_group_row)
        self.worker.load_finished.connect(self.on_load_finished)
        self.worker.error.connect(self.on_worker_error)
        self.worker.finished.connect(lambda: self.set_busy(False))

        self.set_busy(True)
        self.worker.start()

    def add_group_row(self, group: GroupInfo):
        row = self.table.rowCount()
        self.table.insertRow(row)

        check_item = QTableWidgetItem()
        check_item.setFlags(check_item.flags() | Qt.ItemIsUserCheckable)
        check_item.setCheckState(Qt.Unchecked)

        self.table.setItem(row, 0, check_item)
        self.table.setItem(row, 1, QTableWidgetItem(group.title))
        self.table.setItem(row, 2, QTableWidgetItem(group.group_type))
        self.table.setItem(row, 3, QTableWidgetItem(str(group.peer_id)))
        self.table.setItem(row, 4, QTableWidgetItem(group.username or ""))
        self.table.setItem(row, 5, QTableWidgetItem("" if group.members_count is None else str(group.members_count)))
        self.table.setItem(row, 6, QTableWidgetItem(str(group.megagroup)))
        self.table.setItem(row, 7, QTableWidgetItem("Loaded"))

        self.peer_id_to_row[int(group.peer_id)] = row

    def on_load_finished(self, count: int):
        self.append_log(f"Loaded {count} groups/supergroups.")
        self.status_label.setText(f"Loaded {count} groups/supergroups.")
        self.send_button.setEnabled(count > 0)

    def selected_peer_ids(self) -> List[int]:
        ids = []

        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.Checked:
                peer_id_item = self.table.item(row, 3)
                if peer_id_item:
                    ids.append(int(peer_id_item.text()))

        return ids

    def send_selected(self):
        selected = self.selected_peer_ids()

        if not selected:
            QMessageBox.information(self, "No selection", "Select at least one group.")
            return

        message = self.message_edit.toPlainText().strip()
        if not message:
            QMessageBox.warning(self, "Empty message", "Message is empty.")
            return

        min_delay = self.min_delay_spin.value()
        max_delay = self.max_delay_spin.value()

        if min_delay > max_delay:
            QMessageBox.warning(self, "Invalid delay", "Min delay cannot be greater than max delay.")
            return

        if not self.dry_run_checkbox.isChecked():
            result = QMessageBox.question(
                self,
                "Confirm sending",
                f"You are about to send a message to {len(selected)} selected groups.\n\nContinue?",
            )
            if result != QMessageBox.Yes:
                return

        try:
            api_id, api_hash = self.parse_api_settings()
        except Exception as e:
            QMessageBox.warning(self, "Invalid settings", str(e))
            return

        self.append_log(f"Starting send job. Selected groups: {len(selected)}")
        self.status_label.setText("Sending...")

        self.worker = TelegramWorker(
            mode="send",
            api_id=api_id,
            api_hash=api_hash,
            message=message,
            selected_peer_ids=selected,
            dry_run=self.dry_run_checkbox.isChecked(),
            min_delay=min_delay,
            max_delay=max_delay,
            min_members=self.min_members_spin.value(),
        )
        self.worker.log.connect(self.append_log)
        self.worker.send_progress.connect(self.update_send_status)
        self.worker.send_finished.connect(self.on_send_finished)
        self.worker.error.connect(self.on_worker_error)
        self.worker.finished.connect(lambda: self.set_busy(False))

        self.set_busy(True)
        self.worker.start()

    def update_send_status(self, peer_id: int, status: str):
        row = self.peer_id_to_row.get(int(peer_id))
        if row is not None:
            self.table.setItem(row, 7, QTableWidgetItem(status))

    def on_send_finished(self, sent: int, attempted: int, total_members: int):
        text = f"Finished. Sent={sent}, attempted={attempted}, total_members={total_members}"
        self.append_log(text)
        self.status_label.setText(text)

    def on_worker_error(self, error_text: str):
        self.append_log(f"ERROR: {error_text}")
        QMessageBox.critical(self, "Worker error", error_text)

    def stop_worker(self):
        if self.worker:
            self.worker.stop()
            self.append_log("Stop requested...")
            self.status_label.setText("Stopping...")

    def set_all_selected(self, selected: bool):
        state = Qt.Checked if selected else Qt.Unchecked

        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                item.setCheckState(state)


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()