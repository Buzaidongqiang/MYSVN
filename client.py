#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
My-SVN 客户端
基于 PyQt5, UDP 自动发现, 增量传输, 乐观锁冲突检测, AI 备注
双击运行即可，无需任何配置
"""

import os
import sys
import traceback

# 捕获启动阶段的所有异常写入日志
log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash.log")

try:
    import json
    import hashlib
    import socket
    import shutil
    import zipfile
    import io
    import time
    from pathlib import Path
    from datetime import datetime
    from typing import Optional

    import requests
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
        QFileDialog, QMessageBox, QGroupBox, QFormLayout, QHeaderView,
        QProgressBar, QStatusBar, QDialog, QDialogButtonBox, QStyleFactory,
        QCheckBox, QTreeWidget, QTreeWidgetItem, QMenu, QAction, QScrollArea,
    )
    from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSettings
    from PyQt5.QtGui import QFont
except Exception as e:
    with open(log_file, "w", encoding="utf-8") as f:
        f.write(f"IMPORT ERROR: {e}\n{traceback.format_exc()}")
    print(f"启动失败！错误已写入: {log_file}")
    print(f"错误信息: {e}")
    input("按回车键退出...")
    sys.exit(1)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
UDP_LISTEN_PORT = 9999
UDP_DISCOVER_TIMEOUT = 3
HTTP_TIMEOUT = 30
UPLOAD_TIMEOUT = 300

# 自动忽略的目录/文件
IGNORE_PATTERNS = {
    # 通用忽略项
    "node_modules", ".git", "__pycache__", ".tmp", ".log",
    ".svn", ".hg", ".idea", ".vscode", "venv", ".venv",
    "env", ".env", "dist", "build", ".next", "target",
    ".mypy_cache", ".pytest_cache", ".tox", ".eggs",
    "*.pyc", "*.pyo", "*.log", "*.tmp", "*.swp", "*.bak",
    "Thumbs.db", ".DS_Store", "desktop.ini", ".mysvn_manifest.json",
    # UE5 忽略项
    "Intermediate", "Saved", "DerivedDataCache", ".vs", "Binaries",
}


# ---------------------------------------------------------------------------
# 工作线程
# ---------------------------------------------------------------------------
class DiscoverWorker(QThread):
    """UDP 自动发现服务器"""
    found = pyqtSignal(str, int)
    finished_search = pyqtSignal()

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(UDP_DISCOVER_TIMEOUT)
        try:
            sock.bind(("0.0.0.0", UDP_LISTEN_PORT))
        except OSError:
            sock.close()
            self.finished_search.emit()
            return

        start = time.time()
        while time.time() - start < UDP_DISCOVER_TIMEOUT:
            try:
                data, addr = sock.recvfrom(4096)
                msg = json.loads(data.decode("utf-8"))
                if msg.get("server") == "MySVN":
                    self.found.emit(msg["ip"], msg["port"])
            except socket.timeout:
                break
            except Exception:
                continue
        try:
            sock.close()
        except Exception:
            pass
        self.finished_search.emit()


class ScanWorker(QThread):
    """后台扫描本地文件夹，计算 MD5 和文件大小"""
    progress = pyqtSignal(int, int)
    finished_scan = pyqtSignal(dict, dict)
    error = pyqtSignal(str)

    def __init__(self, folder: str):
        super().__init__()
        self.folder = folder

    def _should_ignore(self, name: str) -> bool:
        for pat in IGNORE_PATTERNS:
            if pat.startswith("*"):
                if name.endswith(pat[1:]):
                    return True
            elif name == pat:
                return True
        return False

    def run(self):
        manifest = {}
        file_sizes = {}
        folder_path = Path(self.folder)
        all_files = []
        try:
            for root, dirs, files in os.walk(str(folder_path)):
                dirs[:] = [d for d in dirs if not self._should_ignore(d)]
                for f in files:
                    if not self._should_ignore(f):
                        all_files.append(Path(root) / f)
        except Exception as e:
            self.error.emit(f"扫描文件夹失败: {e}")
            return

        total = len(all_files)
        for i, fpath in enumerate(all_files):
            try:
                rel = fpath.relative_to(folder_path)
                rel_str = str(rel).replace("\\", "/")
                size = fpath.stat().st_size
                h = hashlib.md5()
                with open(fpath, "rb") as fh:
                    for chunk in iter(lambda: fh.read(8192), b""):
                        h.update(chunk)
                manifest[rel_str] = h.hexdigest()
                file_sizes[rel_str] = size
            except Exception as e:
                self.error.emit(f"扫描文件失败: {fpath.name}: {e}")
            self.progress.emit(i + 1, total)
        self.finished_scan.emit(manifest, file_sizes)


class UploadWorker(QThread):
    """后台提交版本"""
    progress = pyqtSignal(str)
    finished_upload = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, server_url: str, folder: str, manifest: dict,
                 username: str, message: str, base_version: int, project_name: str,
                 selected_files: list = None):
        super().__init__()
        self.server_url = server_url.rstrip("/")
        self.folder = folder
        self.manifest = manifest
        self.username = username
        self.message = message
        self.base_version = base_version
        self.project_name = project_name
        self.selected_files = selected_files

    def run(self):
        try:
            self.progress.emit("正在对比文件清单...")
            resp = requests.post(
                f"{self.server_url}/api/check_files",
                json={
                    "manifest": self.manifest,
                    "base_version": self.base_version,
                    "project_name": self.project_name,
                },
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            result = resp.json()

            if result.get("error"):
                self.error.emit(result["error"])
                return

            conflicts = result.get("conflicts", [])
            if conflicts:
                file_list = "\n".join(f"  - {f}" for f in conflicts[:20])
                extra = f"\n  ...还有 {len(conflicts)-20} 个冲突文件" if len(conflicts) > 20 else ""
                self.error.emit(
                    f"冲突检测：以下文件已被其他用户修改，请先还原到最新版本再修改：\n{file_list}{extra}"
                )
                return

            need_upload = result.get("need_upload", [])
            if self.selected_files is not None:
                need_upload = [f for f in need_upload if f in self.selected_files]

            if not need_upload:
                self.error.emit("没有检测到任何变更")
                return

            self.progress.emit(f"正在打包 {len(need_upload)} 个变更文件...")
            zip_buf = io.BytesIO()
            folder_path = Path(self.folder)
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for rel_str in need_upload:
                    fpath = folder_path / rel_str
                    if fpath.exists() and fpath.is_file():
                        zf.write(str(fpath), rel_str)
            zip_buf.seek(0)

            self.progress.emit("正在上传...")
            resp = requests.post(
                f"{self.server_url}/api/upload",
                files={"file": ("changes.zip", zip_buf, "application/zip")},
                data={
                    "username": self.username,
                    "message": self.message,
                    "base_version": str(self.base_version),
                    "project_name": self.project_name,
                },
                timeout=UPLOAD_TIMEOUT,
            )
            resp.raise_for_status()
            upload_result = resp.json()

            if upload_result.get("error"):
                self.error.emit(upload_result["error"])
            else:
                self.finished_upload.emit(upload_result)
        except requests.ConnectionError:
            self.error.emit("无法连接到服务器，请检查服务器地址和端口")
        except requests.Timeout:
            self.error.emit("连接服务器超时，请重试")
        except Exception as e:
            self.error.emit(f"上传失败: {str(e)}")


class RollbackWorker(QThread):
    """增量还原版本 — 仅备份/下载变更文件"""
    progress = pyqtSignal(str)
    finished_rollback = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, server_url: str, version_id: int, local_folder: str,
                 backup_dir: str, local_manifest: dict):
        super().__init__()
        self.server_url = server_url.rstrip("/")
        self.version_id = version_id
        self.local_folder = local_folder
        self.backup_dir = backup_dir
        self.local_manifest = local_manifest

    def run(self):
        try:
            local_path = Path(self.local_folder)
            if not local_path.exists():
                self.error.emit("本地文件夹不存在")
                return

            self.progress.emit("正在获取目标版本文件列表...")
            resp = requests.get(
                f"{self.server_url}/api/version_files/{self.version_id}",
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                self.error.emit(data["error"])
                return
            target_files = {f["path"]: f["md5"] for f in data.get("files", [])}

            self.progress.emit("正在对比文件差异...")
            to_backup = []       # 本地有但将被覆盖/删除的文件
            to_download = []     # 需要从服务端拉取的文件
            to_delete = []       # 目标版本没有、本地有的文件
            new_manifest = dict(target_files)

            # 用本地 manifest 做快速对比
            if self.local_manifest:
                for rel_path, local_md5 in self.local_manifest.items():
                    target_md5 = target_files.get(rel_path)
                    if target_md5 is None:
                        if (local_path / rel_path).exists():
                            to_backup.append(rel_path)
                            to_delete.append(rel_path)
                    elif target_md5 != local_md5:
                        if (local_path / rel_path).exists():
                            to_backup.append(rel_path)
                        to_download.append(rel_path)
                for rel_path in target_files:
                    if rel_path not in self.local_manifest:
                        to_download.append(rel_path)
            else:
                # 无 manifest，做全量扫描对比
                to_download = list(target_files.keys())
                for fpath in local_path.rglob("*"):
                    if fpath.is_file():
                        rel = str(fpath.relative_to(local_path)).replace("\\", "/")
                        to_backup.append(rel)

            # 备份变更/将被删除的文件
            backup_folder = None
            if to_backup:
                backup_base = Path(self.backup_dir) if self.backup_dir else local_path.parent
                backup_base.mkdir(parents=True, exist_ok=True)
                backup_folder = backup_base / f"{local_path.name}_backup_v{self.version_id}_{datetime.now():%Y%m%d_%H%M%S}"
                backup_folder.mkdir(parents=True, exist_ok=True)

                self.progress.emit(f"正在备份 {len(to_backup)} 个变更文件...")
                for rel in to_backup:
                    src = local_path / rel
                    if src.exists():
                        dst = backup_folder / rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(src), str(dst))
                    # 备份后删除本地文件
                    try:
                        if src.is_file():
                            src.unlink(missing_ok=True)
                    except Exception:
                        pass

            # 下载增量文件
            if to_download:
                self.progress.emit(f"正在下载 {len(to_download)} 个文件...")
                # 分批请求，每批最多 500 个文件
                batch_size = 500
                for i in range(0, len(to_download), batch_size):
                    batch = to_download[i:i + batch_size]
                    resp = requests.post(
                        f"{self.server_url}/api/download_selective/{self.version_id}",
                        json={"files": batch},
                        timeout=UPLOAD_TIMEOUT,
                    )
                    resp.raise_for_status()
                    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                        zf.extractall(str(local_path))

            # 删除目标版本中不存在的文件（已在备份后删除，这里处理空目录）
            if to_delete:
                for rel in to_delete:
                    p = local_path / rel
                    if p.exists():
                        try:
                            p.unlink(missing_ok=True)
                        except Exception:
                            pass

            # 保存新的本地 manifest
            self.progress.emit("正在保存本地索引...")
            mp = local_path / ".mysvn_manifest.json"
            mp.write_text(
                json.dumps({"version_id": self.version_id, "files": new_manifest}, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

            self.finished_rollback.emit(str(backup_folder) if backup_folder else "")
        except requests.ConnectionError:
            self.error.emit("无法连接到服务器，请检查服务器地址和端口")
        except requests.Timeout:
            self.error.emit("下载超时，请重试")
        except zipfile.BadZipFile:
            self.error.emit("下载的版本文件已损坏")
        except Exception as e:
            self.error.emit(f"还原失败: {str(e)}")


class FetchVersionsWorker(QThread):
    """后台获取版本列表"""
    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, server_url: str, project_name: str = "default"):
        super().__init__()
        self.server_url = server_url.rstrip("/")
        self.project_name = project_name

    def run(self):
        try:
            resp = requests.get(
                f"{self.server_url}/api/versions?project={self.project_name}",
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                self.error.emit(data["error"])
            else:
                self.finished.emit(data.get("versions", []))
        except requests.ConnectionError:
            self.error.emit("无法连接到服务器")
        except Exception as e:
            self.error.emit(str(e))


# ---------------------------------------------------------------------------
# AI 备注生成
# ---------------------------------------------------------------------------
def generate_ai_message(changed_files: list, api_key: str, model: str = "deepseek-chat") -> Optional[str]:
    if not api_key or not changed_files:
        return None

    file_list = "\n".join(f"- {f}" for f in changed_files[:30])
    prompt = f"""以下是本次代码变更的文件列表，请用一句简洁的中文（不超过30字）总结本次提交的内容和目的：

{file_list}

仅输出中文摘要文本，不要包含任何额外说明。"""

    try:
        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 60,
                "temperature": 0.7,
            },
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        text = result["choices"][0]["message"]["content"].strip()
        return text if text else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 设置对话框
# ---------------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setFixedSize(520, 280)
        self.settings = QSettings("MySVN", "Client")

        layout = QFormLayout(self)

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("输入 DeepSeek API Key")
        self.api_key_edit.setText(self.settings.value("deepseek_api_key", ""))
        layout.addRow("DeepSeek API Key:", self.api_key_edit)

        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("默认: deepseek-chat")
        self.model_edit.setText(self.settings.value("deepseek_model", "deepseek-chat"))
        layout.addRow("模型名称:", self.model_edit)

        backup_row = QHBoxLayout()
        self.backup_edit = QLineEdit()
        self.backup_edit.setReadOnly(True)
        self.backup_edit.setPlaceholderText("留空则备份到工程文件夹同级目录")
        self.backup_edit.setText(self.settings.value("backup_dir", ""))
        backup_row.addWidget(self.backup_edit, 1)
        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(self._browse_backup)
        backup_row.addWidget(browse_btn)
        layout.addRow("备份目录:", backup_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save_and_close)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _browse_backup(self):
        folder = QFileDialog.getExistingDirectory(self, "选择备份目录")
        if folder:
            self.backup_edit.setText(folder)

    def _save_and_close(self):
        self.settings.setValue("deepseek_api_key", self.api_key_edit.text().strip())
        self.settings.setValue("deepseek_model", self.model_edit.text().strip())
        self.settings.setValue("backup_dir", self.backup_edit.text().strip())
        self.accept()


# ---------------------------------------------------------------------------
# 提交预览对话框
# ---------------------------------------------------------------------------
LARGE_FILE_THRESHOLD = 50 * 1024 * 1024

def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size/1024:.1f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size/1024/1024:.1f} MB"
    return f"{size/1024/1024/1024:.1f} GB"


def _guess_ext_category(rel_path: str) -> str:
    ext = os.path.splitext(rel_path)[1].lower()
    cat_map = {
        ".uasset": "蓝图/Asset",
        ".umap": "关卡/Map",
        ".cpp": "C++ 源文件",
        ".h": "C++ 头文件",
        ".cs": "C# 脚本",
        ".ini": "配置文件",
        ".json": "JSON",
        ".py": "Python",
        ".png": "PNG 贴图",
        ".jpg": "JPG 贴图",
        ".jpeg": "JPG 贴图",
        ".tga": "TGA 贴图",
        ".tif": "TIFF 贴图",
        ".bmp": "BMP 贴图",
        ".exr": "EXR 贴图",
        ".wav": "WAV 音频",
        ".mp3": "MP3 音频",
        ".ogg": "OGG 音频",
        ".fbx": "FBX 模型",
        ".obj": "OBJ 模型",
        ".blend": "Blender",
        ".mp4": "MP4 视频",
        ".avi": "AVI 视频",
        ".dll": "DLL 库",
        ".exe": "可执行文件",
    }
    for key, label in cat_map.items():
        if ext == key or ext.startswith(key):
            return label
    return f"{ext} 文件" if ext else "无后缀"


class CommitFileDialog(QDialog):
    """提交文件勾选对话框"""
    def __init__(self, file_sizes: dict, need_upload: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择要提交的文件")
        self.resize(700, 500)

        self.file_sizes = file_sizes
        self.need_upload = need_upload
        self.checks = {}
        self.message = ""
        self.confirmed = False
        self.selected_files = []

        self._init_ui()
        self._populate_files()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        layout.addWidget(QLabel("<b>变更文件列表 — 勾选需要提交的文件：</b>"))

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["文件路径", "大小", "类型"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QTreeWidget.NoSelection)
        layout.addWidget(self.tree, 1)

        btn_row = QHBoxLayout()
        select_all = QPushButton("全选")
        select_all.clicked.connect(lambda: self._toggle_all(True))
        btn_row.addWidget(select_all)
        deselect_all = QPushButton("取消全选")
        deselect_all.clicked.connect(lambda: self._toggle_all(False))
        btn_row.addWidget(deselect_all)
        btn_row.addStretch()
        self.info_label = QLabel()
        btn_row.addWidget(self.info_label)
        layout.addLayout(btn_row)

        layout.addWidget(QLabel("版本备注:"))
        self.msg_edit = QLineEdit()
        self.msg_edit.setPlaceholderText("输入本次提交的备注信息...")
        layout.addWidget(self.msg_edit)

        btn_layout = QHBoxLayout()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addStretch()
        ok_btn = QPushButton("确认提交")
        ok_btn.setMinimumWidth(100)
        ok_btn.clicked.connect(self._confirm)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

    def _populate_files(self):
        groups = {}
        for rel in self.need_upload:
            cat = _guess_ext_category(rel)
            if cat not in groups:
                groups[cat] = []
            groups[cat].append(rel)

        for cat in sorted(groups.keys()):
            cat_item = QTreeWidgetItem(self.tree, [cat, "", f"{len(groups[cat])} 个文件"])
            cat_item.setFlags(cat_item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            cat_item.setCheckState(0, Qt.Checked)
            cat_item.setExpanded(True)

            for fpath in groups[cat]:
                sz = self.file_sizes.get(fpath, 0)
                ch = QCheckBox()
                ch.setChecked(True)
                ch.stateChanged.connect(lambda s, p=fpath: self._on_check_changed(p, s == Qt.Checked))

                item = QTreeWidgetItem(cat_item, [
                    fpath,
                    _format_size(sz),
                    _guess_ext_category(fpath)
                ])
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                item.setCheckState(0, Qt.Checked)

                container = QWidget()
                container_layout = QHBoxLayout(container)
                container_layout.setContentsMargins(0, 0, 0, 0)
                container_layout.addWidget(ch)
                self.tree.setItemWidget(item, 0, container)
                self.checks[fpath] = True

        self._update_info()

    def _toggle_all(self, checked: bool):
        for fpath in list(self.checks.keys()):
            self.checks[fpath] = checked
        for i in range(self.tree.topLevelItemCount()):
            cat_item = self.tree.topLevelItem(i)
            cat_item.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
            for j in range(cat_item.childCount()):
                child = cat_item.child(j)
                child.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
                widget = self.tree.itemWidget(child, 0)
                if widget:
                    cb = widget.findChild(QCheckBox)
                    if cb:
                        cb.setChecked(checked)
        self._update_info()

    def _on_check_changed(self, fpath: str, checked: bool):
        self.checks[fpath] = checked
        self._update_info()

    def _update_info(self):
        selected = sum(1 for v in self.checks.values() if v)
        total_size = sum(
            self.file_sizes.get(f, 0) for f, v in self.checks.items() if v
        )
        self.info_label.setText(f"已选: {selected}/{len(self.need_upload)} 个文件, {_format_size(total_size)}")

    def _confirm(self):
        self.selected_files = [f for f, v in self.checks.items() if v]
        if not self.selected_files:
            QMessageBox.warning(self, "提示", "请至少选择一个文件提交")
            return
        self.message = self.msg_edit.text().strip() or "无备注"
        self.confirmed = True
        self.accept()


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("My-SVN 轻量级局域网版本管理工具")
        self.setMinimumSize(920, 620)

        self.server_url = ""
        self.local_folder = ""
        self.project_name = ""
        self.username = os.getenv("USERNAME") or os.getenv("USER") or "anonymous"
        self.current_version = 0
        self.settings = QSettings("MySVN", "Client")

        self._init_ui()
        self._restore_settings()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(8)

        # ---- 菜单栏 ----
        menubar = self.menuBar()
        settings_action = menubar.addAction("\u2699 设置")
        settings_action.triggered.connect(self._open_settings)

        # ---- 服务器连接区 ----
        conn_group = QGroupBox("服务器连接")
        conn_layout = QHBoxLayout(conn_group)

        conn_layout.addWidget(QLabel("地址:"))
        self.addr_edit = QLineEdit()
        self.addr_edit.setPlaceholderText("192.168.1.x:5000")
        conn_layout.addWidget(self.addr_edit, 1)

        self.discover_btn = QPushButton("\U0001f50d 自动搜索服务器")
        self.discover_btn.clicked.connect(self._discover_server)
        conn_layout.addWidget(self.discover_btn)

        self.connect_btn = QPushButton("连接")
        self.connect_btn.clicked.connect(self._connect_server)
        conn_layout.addWidget(self.connect_btn)

        self.conn_status = QLabel("未连接")
        self.conn_status.setStyleSheet("color: red;")
        conn_layout.addWidget(self.conn_status)

        main_layout.addWidget(conn_group)

        # ---- 本地文件夹区 ----
        folder_group = QGroupBox("本地工程")
        folder_layout = QHBoxLayout(folder_group)

        folder_layout.addWidget(QLabel("文件夹:"))
        self.folder_edit = QLineEdit()
        self.folder_edit.setReadOnly(True)
        self.folder_edit.setPlaceholderText("选择本地工程文件夹...")
        folder_layout.addWidget(self.folder_edit, 1)

        self.browse_btn = QPushButton("浏览...")
        self.browse_btn.clicked.connect(self._browse_folder)
        folder_layout.addWidget(self.browse_btn)

        main_layout.addWidget(folder_group)

        # ---- 版本列表 ----
        list_group = QGroupBox("版本历史")
        list_layout = QVBoxLayout(list_group)

        self.version_table = QTableWidget()
        self.version_table.setColumnCount(4)
        self.version_table.setHorizontalHeaderLabels(["版本ID", "提交人", "提交时间", "备注"])
        self.version_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.version_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.version_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.version_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.version_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.version_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.version_table.setAlternatingRowColors(True)
        self.version_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.version_table.customContextMenuRequested.connect(self._on_version_menu)
        list_layout.addWidget(self.version_table)

        self.refresh_btn = QPushButton("\U0001f504 刷新版本列表")
        self.refresh_btn.clicked.connect(self._refresh_versions)
        list_layout.addWidget(self.refresh_btn)

        main_layout.addWidget(list_group, 1)

        # ---- 操作按钮区 ----
        action_layout = QHBoxLayout()

        self.commit_btn = QPushButton("\U0001f4be 保存版本 (Commit)")
        self.commit_btn.setMinimumHeight(36)
        self.commit_btn.clicked.connect(self._commit)
        action_layout.addWidget(self.commit_btn)

        self.ai_btn = QPushButton("\U0001f916 AI 自动生成备注")
        self.ai_btn.setMinimumHeight(36)
        self.ai_btn.clicked.connect(self._ai_generate_message)
        action_layout.addWidget(self.ai_btn)

        self.rollback_btn = QPushButton("\U0001f504 还原选中版本")
        self.rollback_btn.setMinimumHeight(36)
        self.rollback_btn.clicked.connect(self._rollback)
        action_layout.addWidget(self.rollback_btn)

        main_layout.addLayout(action_layout)

        # ---- 进度条和状态栏 ----
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        main_layout.addWidget(self.progress_bar)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪")

        self._update_button_states()

    def _update_button_states(self):
        connected = bool(self.server_url)
        has_folder = bool(self.local_folder)
        self.commit_btn.setEnabled(connected and has_folder)
        self.ai_btn.setEnabled(connected and has_folder)
        self.rollback_btn.setEnabled(connected and has_folder)
        self.refresh_btn.setEnabled(connected)

    # ------------------------------------------------------------------
    # 设置持久化
    # ------------------------------------------------------------------
    def _restore_settings(self):
        last_addr = self.settings.value("last_server_address", "")
        if last_addr:
            self.addr_edit.setText(last_addr)
        last_folder = self.settings.value("last_folder", "")
        if last_folder and os.path.isdir(last_folder):
            self.folder_edit.setText(last_folder)
            self.local_folder = last_folder
            self.project_name = os.path.basename(last_folder)

    def _save_settings(self):
        self.settings.setValue("last_server_address", self.addr_edit.text().strip())
        self.settings.setValue("last_folder", self.local_folder)

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------
    def _open_settings(self):
        dlg = SettingsDialog(self)
        dlg.exec_()

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择本地工程文件夹")
        if folder:
            self.local_folder = folder
            self.project_name = os.path.basename(folder)
            self.folder_edit.setText(folder)
            self._save_settings()
            self._update_button_states()
            self._refresh_versions()
            self.status_bar.showMessage(f"已选择: {self.project_name}")

    def _discover_server(self):
        self.discover_btn.setEnabled(False)
        self.discover_btn.setText("搜索中...")
        self.status_bar.showMessage("正在搜索局域网内的 My-SVN 服务器...")

        self.discover_worker = DiscoverWorker()
        self.discover_worker.found.connect(self._on_server_found)
        self.discover_worker.finished_search.connect(self._on_discover_finished)
        self.discover_worker.start()

    def _on_server_found(self, ip: str, port: int):
        self.addr_edit.setText(f"{ip}:{port}")
        self.status_bar.showMessage(f"发现服务器: {ip}:{port}")

    def _on_discover_finished(self):
        self.discover_btn.setEnabled(True)
        self.discover_btn.setText("\U0001f50d 自动搜索服务器")
        if not self.addr_edit.text():
            self.status_bar.showMessage("未发现服务器，请检查服务端是否启动")
            QMessageBox.information(
                self, "搜索结果",
                "未发现 My-SVN 服务器。\n\n请确认：\n"
                "1. 服务端已启动\n"
                "2. 客户端与服务端在同一局域网\n"
                "3. 防火墙未阻止 UDP 9999 端口\n\n"
                "你也可以手动输入服务器地址。"
            )

    def _connect_server(self):
        addr = self.addr_edit.text().strip()
        if not addr:
            self.conn_status.setText("请输入地址")
            return
        if not addr.startswith("http"):
            addr = f"http://{addr}"
        self.server_url = addr
        self.conn_status.setText("已连接")
        self.conn_status.setStyleSheet("color: green;")
        self._save_settings()
        self._update_button_states()
        self.status_bar.showMessage(f"已连接到 {addr} — 请选择本地工程文件夹")

    def _refresh_versions(self):
        if not self.server_url or not self.project_name:
            return
        self.status_bar.showMessage("正在获取版本列表...")
        self.worker = FetchVersionsWorker(self.server_url, self.project_name)
        self.worker.finished.connect(self._on_versions_loaded)
        self.worker.error.connect(self._on_versions_error)
        self.worker.start()

    def _on_versions_loaded(self, versions: list):
        self.version_table.setRowCount(len(versions))
        for i, v in enumerate(versions):
            self.version_table.setItem(i, 0, QTableWidgetItem(str(v["id"])))
            self.version_table.setItem(i, 1, QTableWidgetItem(v["username"]))
            self.version_table.setItem(i, 2, QTableWidgetItem(v["commit_time"]))
            self.version_table.setItem(i, 3, QTableWidgetItem(v["message"]))
        if versions:
            self.current_version = int(versions[0]["id"])
        self.status_bar.showMessage(f"已加载 {len(versions)} 个版本")

    def _on_versions_error(self, msg: str):
        self.status_bar.showMessage(f"获取版本失败: {msg}")

    # ------------------------------------------------------------------
    # 提交版本
    # ------------------------------------------------------------------
    def _commit(self):
        if not self.local_folder:
            QMessageBox.warning(self, "提示", "请先选择本地工程文件夹")
            return

        self.commit_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(0)
        self.status_bar.showMessage("正在扫描本地文件...")

        self.scan_worker = ScanWorker(self.local_folder)
        self.scan_worker.progress.connect(self._on_scan_progress)
        self.scan_worker.finished_scan.connect(self._on_scan_done)
        self.scan_worker.error.connect(self._on_scan_error)
        self.scan_worker.start()

    def _on_scan_progress(self, current: int, total: int):
        if self.progress_bar.maximum() == 0:
            self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self.status_bar.showMessage(f"扫描文件: {current}/{total}")

    def _on_scan_done(self, manifest: dict, file_sizes: dict):
        self.status_bar.showMessage(f"扫描完成，共 {len(manifest)} 个文件，正在对比服务端...")
        self._pending_manifest = manifest
        self._pending_file_sizes = file_sizes

        server_url = self.server_url
        current_version = self.current_version
        project_name = self.project_name

        class CheckWorker(QThread):
            result = pyqtSignal(dict)

            def run(self):
                try:
                    resp = requests.post(
                        f"{server_url}/api/check_files",
                        json={"manifest": manifest, "base_version": current_version, "project_name": project_name},
                        timeout=HTTP_TIMEOUT,
                    )
                    resp.raise_for_status()
                    self.result.emit(resp.json())
                except Exception as e:
                    self.result.emit({"error": str(e)})

        self.check_worker = CheckWorker()
        self.check_worker.result.connect(self._on_check_result)
        self.check_worker.start()

    def _on_check_result(self, result: dict):
        self.progress_bar.setVisible(False)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.commit_btn.setEnabled(True)

        if result.get("error"):
            QMessageBox.critical(self, "错误", f"无法连接服务器: {result['error']}")
            self.status_bar.showMessage("对比失败")
            return

        conflicts = result.get("conflicts", [])
        if conflicts:
            file_list = "\n".join(f"  - {f}" for f in conflicts[:20])
            extra = f"\n  ...还有 {len(conflicts)-20} 个冲突文件" if len(conflicts) > 20 else ""
            self.status_bar.showMessage("检测到冲突")
            QMessageBox.critical(self, "冲突检测",
                f"以下文件已被其他用户修改，请先还原到最新版本再修改：\n{file_list}{extra}")
            return

        need_upload = result.get("need_upload", [])
        if not need_upload:
            self.status_bar.showMessage("没有变更")
            QMessageBox.information(self, "提示", "没有检测到任何变更")
            return

        dlg = CommitFileDialog(self._pending_file_sizes, need_upload, self)
        if dlg.exec_() == QDialog.Accepted and dlg.confirmed:
            self._do_upload(self._pending_manifest, dlg.message, dlg.selected_files)

    def _do_upload(self, manifest: dict, message: str, selected_files: list = None):
        self.commit_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(0)
        self.status_bar.showMessage("正在上传...")

        self.upload_worker = UploadWorker(
            self.server_url, self.local_folder, manifest,
            self.username, message, self.current_version,
            self.project_name, selected_files,
        )
        self.upload_worker.progress.connect(self._on_upload_progress)
        self.upload_worker.finished_upload.connect(self._on_upload_done)
        self.upload_worker.error.connect(self._on_upload_error)
        self.upload_worker.start()

    def _on_scan_error(self, msg: str):
        self.status_bar.showMessage(msg)
        self._reset_commit_ui()

    def _on_upload_progress(self, msg: str):
        self.status_bar.showMessage(msg)

    def _on_upload_done(self, result: dict):
        self._reset_commit_ui()
        self._refresh_versions()
        self._save_local_manifest(result["version_id"], result.get("manifest", {}))
        QMessageBox.information(
            self, "提交成功",
            f"版本 v{result['version_id']} 已保存！\n共 {result['file_count']} 个文件。"
        )

    def _on_upload_error(self, msg: str):
        self._reset_commit_ui()
        self.status_bar.showMessage("提交失败")
        QMessageBox.critical(self, "提交失败", msg)

    def _reset_commit_ui(self):
        self.commit_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)

    # ------------------------------------------------------------------
    # 本地 manifest 管理
    # ------------------------------------------------------------------
    def _manifest_path(self) -> Path:
        return Path(self.local_folder) / ".mysvn_manifest.json"

    def _save_local_manifest(self, version_id: int, manifest: dict):
        try:
            data = {"version_id": version_id, "files": manifest}
            self._manifest_path().write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass

    def _load_local_manifest(self) -> tuple:
        """返回 (version_id, {filepath: md5})，无 manifest 返回 (0, {})"""
        mp = self._manifest_path()
        if mp.exists():
            try:
                data = json.loads(mp.read_text(encoding="utf-8"))
                return data.get("version_id", 0), data.get("files", {})
            except Exception:
                pass
        return 0, {}

    # ------------------------------------------------------------------
    # AI 备注
    # ------------------------------------------------------------------
    def _ai_generate_message(self):
        api_key = self.settings.value("deepseek_api_key", "")
        if not api_key:
            QMessageBox.information(
                self, "提示",
                "未配置 DeepSeek API Key。\n请在菜单栏「设置」中配置。"
            )
            return

        if not self.local_folder:
            QMessageBox.warning(self, "提示", "请先选择本地工程文件夹")
            return

        self.status_bar.showMessage("正在扫描文件用于 AI 分析...")
        self.ai_scan_worker = ScanWorker(self.local_folder)
        self.ai_scan_worker.finished_scan.connect(self._on_ai_scan_done)
        self.ai_scan_worker.error.connect(self._on_scan_error)
        self.ai_scan_worker.start()

    def _on_ai_scan_done(self, manifest: dict, file_sizes: dict):
        changed_files = list(manifest.keys())
        api_key = self.settings.value("deepseek_api_key", "")
        model = self.settings.value("deepseek_model", "deepseek-chat")

        self.status_bar.showMessage("正在调用 AI 生成备注...")

        class AiWorker(QThread):
            result_ready = pyqtSignal(str)

            def run(self):
                msg = generate_ai_message(changed_files, api_key, model)
                self.result_ready.emit(msg or "")

        self._pending_manifest = manifest
        self._pending_file_sizes = file_sizes

        self.ai_worker = AiWorker()
        self.ai_worker.result_ready.connect(self._on_ai_result)
        self.ai_worker.start()

    def _on_ai_result(self, text: str):
        self.status_bar.showMessage("AI 备注生成完成")
        if text:
            self._commit_with_ai_message(text.strip())

    def _commit_with_ai_message(self, message: str):
        manifest = self._pending_manifest
        file_sizes = self._pending_file_sizes
        server_url = self.server_url
        current_version = self.current_version
        project_name = self.project_name

        class CheckWorker(QThread):
            result = pyqtSignal(dict)

            def run(self):
                try:
                    resp = requests.post(
                        f"{server_url}/api/check_files",
                        json={"manifest": manifest, "base_version": current_version, "project_name": project_name},
                        timeout=HTTP_TIMEOUT,
                    )
                    resp.raise_for_status()
                    self.result.emit(resp.json())
                except Exception as e:
                    self.result.emit({"error": str(e)})

        check_worker = CheckWorker()
        check_worker.result.connect(lambda r: self._on_ai_check_result(r, manifest, file_sizes, message))
        check_worker.start()

    def _on_ai_check_result(self, result: dict, manifest: dict, file_sizes: dict, message: str):
        if result.get("error"):
            QMessageBox.critical(self, "错误", str(result["error"]))
            self.status_bar.showMessage("对比失败")
            return

        conflicts = result.get("conflicts", [])
        if conflicts:
            file_list = "\n".join(f"  - {f}" for f in conflicts[:20])
            self.status_bar.showMessage("检测到冲突")
            QMessageBox.critical(self, "冲突检测",
                f"以下文件已被其他用户修改，请先还原到最新版本再修改：\n{file_list}")
            return

        need_upload = result.get("need_upload", [])
        if not need_upload:
            QMessageBox.information(self, "提示", "没有检测到任何变更")
            return

        dlg = CommitFileDialog(file_sizes, need_upload, self)
        dlg.msg_edit.setText(message)
        if dlg.exec_() == QDialog.Accepted and dlg.confirmed:
            self._do_upload(manifest, dlg.message, dlg.selected_files)

    # ------------------------------------------------------------------
    # 还原版本
    # ------------------------------------------------------------------
    def _rollback(self):
        selected = self.version_table.currentRow()
        if selected < 0:
            QMessageBox.warning(self, "提示", "请先在版本列表中选择一个版本")
            return
        version_id = int(self.version_table.item(selected, 0).text())
        self._do_rollback(version_id)

    def _do_rollback(self, version_id: int):
        if not self.local_folder:
            QMessageBox.warning(self, "提示", "请先选择本地工程文件夹")
            return

        row = -1
        for r in range(self.version_table.rowCount()):
            if int(self.version_table.item(r, 0).text()) == version_id:
                row = r
                break
        version_msg = self.version_table.item(row, 3).text() if row >= 0 else ""

        backup_dir = self.settings.value("backup_dir", "")
        backup_info = backup_dir if backup_dir else "工程同级目录"

        _, local_manifest = self._load_local_manifest()

        reply = QMessageBox.question(
            self, "确认还原",
            f"即将把本地文件夹还原到版本 v{version_id}。\n\n"
            f"备注: {version_msg}\n\n"
            f"备份位置: {backup_info}\n"
            "仅备份被覆盖/删除的文件到文件夹。\n\n"
            "确定要继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self.rollback_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(0)

        self.rollback_worker = RollbackWorker(
            self.server_url, version_id, self.local_folder, backup_dir, local_manifest,
        )
        self.rollback_worker.progress.connect(self._on_rollback_progress)
        self.rollback_worker.finished_rollback.connect(self._on_rollback_done)
        self.rollback_worker.error.connect(self._on_rollback_error)
        self.rollback_worker.start()

    def _on_rollback_progress(self, msg: str):
        self.status_bar.showMessage(msg)

    def _on_rollback_done(self, backup_path: str):
        self.rollback_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.status_bar.showMessage("还原完成")
        QMessageBox.information(
            self, "还原成功",
            f"本地文件已还原到指定版本。\n\n备份位置: {backup_path}"
        )

    def _on_rollback_error(self, msg: str):
        self.rollback_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.status_bar.showMessage("还原失败")
        QMessageBox.critical(self, "还原失败", msg)

    # ------------------------------------------------------------------
    # 右键菜单
    # ------------------------------------------------------------------
    def _on_version_menu(self, pos):
        item = self.version_table.itemAt(pos)
        if not item:
            return
        row = item.row()
        version_id = int(self.version_table.item(row, 0).text())
        version_msg = self.version_table.item(row, 3).text()

        menu = QMenu(self)
        action_restore = QAction("还原到当前文件夹", self)
        action_restore.triggered.connect(lambda: self._rollback_version(version_id))
        menu.addAction(action_restore)

        action_download = QAction("下载到指定目录...", self)
        action_download.triggered.connect(lambda: self._download_version(version_id))
        menu.addAction(action_download)

        menu.addSeparator()

        action_files = QAction("查看文件列表", self)
        action_files.triggered.connect(lambda: self._show_version_files(version_id, version_msg))
        menu.addAction(action_files)

        menu.addSeparator()

        action_delete = QAction("删除版本", self)
        action_delete.triggered.connect(lambda: self._delete_version(version_id))
        menu.addAction(action_delete)

        menu.exec_(self.version_table.viewport().mapToGlobal(pos))

    def _rollback_version(self, version_id: int):
        self._do_rollback(version_id)

    def _download_version(self, version_id: int):
        folder = QFileDialog.getExistingDirectory(self, "选择下载目标目录")
        if not folder:
            return

        self.status_bar.showMessage(f"正在下载版本 v{version_id}...")

        dl_url = self.server_url

        class DownloadWorker(QThread):
            progress = pyqtSignal(str)
            done = pyqtSignal()
            error = pyqtSignal(str)

            def run(self):
                try:
                    self.progress.emit("正在下载...")
                    resp = requests.get(
                        f"{dl_url}/api/download/{version_id}",
                        timeout=UPLOAD_TIMEOUT,
                    )
                    resp.raise_for_status()
                    self.progress.emit("正在解压...")
                    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                        zf.extractall(str(folder))
                    self.done.emit()
                except Exception as e:
                    self.error.emit(str(e))

        self.dl_worker = DownloadWorker()
        self.dl_worker.progress.connect(self.status_bar.showMessage)
        self.dl_worker.done.connect(lambda: QMessageBox.information(self, "下载完成", f"版本 v{version_id} 已下载到:\n{folder}"))
        self.dl_worker.error.connect(lambda e: QMessageBox.critical(self, "下载失败", e))
        self.dl_worker.start()

    def _show_version_files(self, version_id: int, version_msg: str):
        self.status_bar.showMessage("正在获取文件列表...")

        sv_url = self.server_url

        class FilesWorker(QThread):
            result = pyqtSignal(list)
            error = pyqtSignal(str)

            def run(self):
                try:
                    resp = requests.get(
                        f"{sv_url}/api/version_files/{version_id}",
                        timeout=HTTP_TIMEOUT,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    if data.get("error"):
                        self.error.emit(data["error"])
                    else:
                        self.result.emit(data.get("files", []))
                except Exception as e:
                    self.error.emit(str(e))

        self.files_worker = FilesWorker()
        self.files_worker.result.connect(lambda files: self._display_files_dialog(version_id, version_msg, files))
        self.files_worker.error.connect(lambda e: QMessageBox.critical(self, "错误", e))
        self.files_worker.start()

    def _display_files_dialog(self, version_id: int, msg: str, files: list):
        dlg = QDialog(self)
        dlg.setWindowTitle(f"版本 v{version_id} 文件列表")
        dlg.resize(600, 450)
        layout = QVBoxLayout(dlg)

        layout.addWidget(QLabel(f"<b>版本 v{version_id}</b> — {msg}"))
        layout.addWidget(QLabel(f"共 {len(files)} 个文件"))

        tree = QTreeWidget()
        tree.setHeaderLabels(["文件路径", "大小"])
        tree.setAlternatingRowColors(True)
        for f in files:
            item = QTreeWidgetItem(tree, [f["path"], _format_size(f["size"])])
        layout.addWidget(tree)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn)

        dlg.exec_()

    def _delete_version(self, version_id: int):
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要永久删除版本 v{version_id} 吗？\n\n此操作不可撤销！",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self.status_bar.showMessage("正在删除版本...")
        sv_url = self.server_url

        class DeleteWorker(QThread):
            result = pyqtSignal(dict)

            def run(self):
                try:
                    resp = requests.post(
                        f"{sv_url}/api/delete_version/{version_id}",
                        timeout=HTTP_TIMEOUT,
                    )
                    resp.raise_for_status()
                    self.result.emit(resp.json())
                except Exception as e:
                    self.result.emit({"error": str(e)})

        self.del_worker = DeleteWorker()
        self.del_worker.result.connect(
            lambda r: self._on_delete_result(r, version_id)
        )
        self.del_worker.start()

    def _on_delete_result(self, result: dict, version_id: int):
        if result.get("error"):
            self.status_bar.showMessage("删除失败")
            QMessageBox.critical(self, "删除失败", result["error"])
            return

        # 清理本地备份（ZIP 文件或文件夹）
        try:
            backup_dir = self.settings.value("backup_dir", "")
            search_dir = Path(backup_dir) if backup_dir else Path(self.local_folder).parent if self.local_folder else None
            if search_dir and search_dir.exists():
                pattern = f"*_backup_v{version_id}_*"
                for f in search_dir.glob(pattern):
                    try:
                        if f.is_dir():
                            shutil.rmtree(str(f), ignore_errors=True)
                        else:
                            f.unlink(missing_ok=True)
                    except Exception:
                        pass
        except Exception:
            pass

        self.status_bar.showMessage(f"版本 v{version_id} 已删除")
        QMessageBox.information(self, "删除成功", f"版本 v{version_id} 已永久删除。\n已清理关联的本地备份文件。")
        self._refresh_versions()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    try:
        app = QApplication(sys.argv)
        app.setStyle(QStyleFactory.create("Fusion"))

        font = QFont("Microsoft YaHei UI", 9)
        app.setFont(font)

        window = MainWindow()
        window.show()
        sys.exit(app.exec_())
    except Exception as e:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"RUNTIME ERROR: {e}\n{traceback.format_exc()}")
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"FATAL: {e}\n{traceback.format_exc()}")
        print(f"启动失败！错误已写入: {log_file}")
        print(f"错误信息: {e}")
        input("按回车键退出...")
