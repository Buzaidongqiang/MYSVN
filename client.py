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
    import subprocess
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
        QStyle, QInputDialog,
    )
    from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSettings, QTimer
    from PyQt5.QtGui import QFont, QColor, QBrush
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
                 selected_files: list = None, force: bool = False):
        super().__init__()
        self.server_url = server_url.rstrip("/")
        self.folder = folder
        self.manifest = manifest
        self.username = username
        self.message = message
        self.base_version = base_version
        self.project_name = project_name
        self.selected_files = selected_files
        self.force = force

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
            if conflicts and not self.force:
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


class FetchProjectsWorker(QThread):
    """后台获取服务端项目列表"""
    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, server_url: str):
        super().__init__()
        self.server_url = server_url.rstrip("/")

    def run(self):
        try:
            resp = requests.get(
                f"{self.server_url}/api/projects",
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                self.error.emit(data["error"])
            else:
                self.finished.emit(data.get("projects", []))
        except requests.ConnectionError:
            self.error.emit("无法连接到服务器")
        except Exception as e:
            self.error.emit(str(e))


class AiRemarkWorker(QThread):
    """后台调用 DeepSeek API 生成提交备注"""
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, file_list: list, api_key: str, model: str = "deepseek-chat"):
        super().__init__()
        self.file_list = file_list
        self.api_key = api_key
        self.model = model

    def run(self):
        try:
            if not self.api_key:
                self.error.emit("未配置 DeepSeek API Key，请在设置中配置")
                return
            if not self.file_list:
                self.error.emit("文件列表为空")
                return
            msg = generate_ai_message(self.file_list, self.api_key, self.model)
            if msg:
                self.finished.emit(msg)
            else:
                self.error.emit("AI 返回为空，请检查 API Key 和网络连接")
        except Exception as e:
            self.error.emit(f"AI 调用出错: {e}")


# ---------------------------------------------------------------------------
# 版本信息
# ---------------------------------------------------------------------------
APP_VERSION = "1.0.2"  # 仅用于显示，更新检测基于 git commit 对比
# 本地 git 仓库路径（用于获取本地 HEAD）
GIT_REPO_PATH = os.path.dirname(os.path.abspath(__file__))
# 远程仓库 commits API（将 main 改为你的默认分支名）
GITHUB_COMMITS_API = "https://api.github.com/repos/Buzaidongqiang/MYSVN/commits/main"

def _get_local_head_sha() -> str:
    """获取本地 git HEAD 的 SHA"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=GIT_REPO_PATH,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""

class UpdateCheckWorker(QThread):
    """后台检查 GitHub 仓库是否有新提交（多源回退）"""
    finished = pyqtSignal(dict)  # {has_update, local_sha, remote_sha, commit_msg, source}
    error = pyqtSignal(str)

    def _try_api(self, local_sha: str):
        """方式 1：GitHub REST API（能获取 commit 摘要）"""
        resp = requests.get(GITHUB_COMMITS_API, timeout=10)
        if resp.status_code == 404:
            return None  # 仓库或分支不存在，交给 git fallback
        resp.raise_for_status()
        data = resp.json()
        remote_sha = data.get("sha", "")
        commit_msg = data.get("commit", {}).get("message", "")
        if not remote_sha:
            return None
        return {
            "has_update": remote_sha != local_sha,
            "local_sha": local_sha[:10],
            "remote_sha": remote_sha[:10],
            "commit_msg": commit_msg.split("\n")[0] if commit_msg else "",
            "source": "GitHub API",
        }

    def _try_git(self, local_sha: str):
        """方式 2：git ls-remote（走 git 网络栈，支持镜像/代理）"""
        result = subprocess.run(
            ["git", "ls-remote", "origin", "refs/heads/main"],
            cwd=GIT_REPO_PATH,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None
        output = result.stdout.strip()
        if not output:
            return None
        # git ls-remote 输出格式: <sha>\trefs/heads/main
        remote_sha = output.split()[0]
        return {
            "has_update": remote_sha != local_sha,
            "local_sha": local_sha[:10],
            "remote_sha": remote_sha[:10],
            "commit_msg": "(请执行 git fetch 查看详情)",
            "source": "git ls-remote",
        }

    def run(self):
        try:
            local_sha = _get_local_head_sha()
            if not local_sha:
                self.error.emit("无法获取本地版本信息，请确保 git 已安装")
                return

            # 优先用 GitHub API（能获取 commit 摘要）
            try:
                result = self._try_api(local_sha)
                if result is not None:
                    self.finished.emit(result)
                    return
            except requests.ConnectionError:
                pass  # API 不可用，回退到 git
            except Exception:
                pass

            # 回退到 git ls-remote
            result = self._try_git(local_sha)
            if result is not None:
                self.finished.emit(result)
                return

            self.error.emit("所有更新源均不可用，请检查网络或手动访问 GitHub")
        except Exception as e:
            self.error.emit(f"检查更新失败: {e}")


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


class ConflictDialog(QDialog):
    """冲突解决对话框 — 显示冲突文件，让用户选择强制覆盖或取消"""
    def __init__(self, conflicts: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("检测到文件冲突")
        self.resize(520, 360)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        layout.addWidget(QLabel(
            "<b style='color:red;'>以下文件已被其他用户修改：</b>"))
        layout.addWidget(QLabel(
            "你的操作会覆盖他人的变更。建议先同步到最新版本后再修改。"))

        tree = QTreeWidget()
        tree.setHeaderLabels(["冲突文件路径"])
        tree.setAlternatingRowColors(True)
        for f in conflicts[:50]:
            item = QTreeWidgetItem(tree, [f])
            item.setToolTip(0, f)
        if len(conflicts) > 50:
            QTreeWidgetItem(tree, [f"... 还有 {len(conflicts)-50} 个冲突文件"])
        layout.addWidget(tree, 1)

        btn_layout = QHBoxLayout()
        cancel_btn = QPushButton("取消提交")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addStretch()

        force_btn = QPushButton("强制覆盖提交")
        force_btn.setStyleSheet(
            "QPushButton { background-color: #e74c3c; color: white; "
            "font-weight: bold; padding: 6px 16px; }"
            "QPushButton:hover { background-color: #c0392b; }"
        )
        force_btn.clicked.connect(self.accept)
        btn_layout.addWidget(force_btn)

        layout.addLayout(btn_layout)


class CommitFileDialog(QDialog):
    """提交文件勾选对话框 — 目录树结构，内置 AI 备注"""
    def __init__(self, file_sizes: dict, need_upload: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择要提交的文件")
        self.resize(720, 540)

        self.file_sizes = file_sizes
        self.need_upload = need_upload
        self.checks = {}           # {file_path: bool}
        self._folder_items = {}    # {folder_path: QTreeWidgetItem}
        self.message = ""
        self.confirmed = False
        self.selected_files = []

        self._init_ui()
        self._populate_tree()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        layout.addWidget(QLabel("<b>变更文件 — 按目录勾选：</b>"))

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["路径/文件", "大小", "类型"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QTreeWidget.NoSelection)
        self.tree.itemChanged.connect(self._on_item_changed)
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

        # 版本备注 + AI 按钮
        msg_row = QHBoxLayout()
        msg_row.addWidget(QLabel("版本备注:"))
        self.msg_edit = QLineEdit()
        self.msg_edit.setPlaceholderText("输入本次提交的备注信息...")
        msg_row.addWidget(self.msg_edit, 1)
        self.ai_btn = QPushButton("\U0001f916 AI 生成")
        self.ai_btn.clicked.connect(self._ai_generate)
        msg_row.addWidget(self.ai_btn)
        layout.addLayout(msg_row)

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

    def _populate_tree(self):
        sorted_files = sorted(self.need_upload, key=lambda x: (x.split("/")[:-1], x))

        for rel_path in sorted_files:
            parts = rel_path.split("/")
            sz = self.file_sizes.get(rel_path, 0)
            self.checks[rel_path] = True

            parent_item = None
            folder_path = ""
            for i, part in enumerate(parts[:-1]):
                folder_path = folder_path + "/" + part if folder_path else part
                if folder_path not in self._folder_items:
                    item = QTreeWidgetItem()
                    item.setText(0, part)
                    item.setText(1, "")
                    item.setText(2, "文件夹")
                    item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsAutoTristate)
                    item.setCheckState(0, Qt.Checked)
                    item.setExpanded(True)
                    if parent_item is None:
                        self.tree.addTopLevelItem(item)
                    else:
                        parent_item.addChild(item)
                    self._folder_items[folder_path] = item
                parent_item = self._folder_items[folder_path]

            file_name = parts[-1]
            file_item = QTreeWidgetItem()
            file_item.setText(0, file_name)
            file_item.setText(1, _format_size(sz))
            file_item.setText(2, _guess_ext_category(rel_path))
            file_item.setFlags(file_item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            file_item.setCheckState(0, Qt.Checked)
            file_item.setData(0, Qt.UserRole, rel_path)

            if parent_item is not None:
                parent_item.addChild(file_item)
            else:
                self.tree.addTopLevelItem(file_item)

        self._update_info()

    def _on_item_changed(self, item, column):
        if column != 0:
            return
        is_checked = item.checkState(0) == Qt.Checked
        self._apply_check_state(item, is_checked)

    def _apply_check_state(self, item, checked: bool):
        rel_path = item.data(0, Qt.UserRole)
        if rel_path and rel_path in self.checks:
            self.checks[rel_path] = checked
        for i in range(item.childCount()):
            child = item.child(i)
            child.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
            self._apply_check_state(child, checked)

    def _toggle_all(self, checked: bool):
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            item.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
            self._apply_check_state(item, checked)
        self.tree.blockSignals(False)
        self._update_info()

    def _update_info(self):
        selected = sum(1 for v in self.checks.values() if v)
        total_size = sum(
            self.file_sizes.get(f, 0) for f, v in self.checks.items() if v
        )
        self.info_label.setText(f"已选: {selected}/{len(self.need_upload)} 个文件, {_format_size(total_size)}")

    def _ai_generate(self):
        """根据当前勾选的文件调用 AI 生成备注"""
        checked_files = [f for f, v in self.checks.items() if v]
        if not checked_files:
            QMessageBox.warning(self, "提示", "请先勾选需要提交的文件")
            return

        parent = self.parent()
        if not parent:
            QMessageBox.warning(self, "提示", "无法获取设置")
            return

        api_key = parent.settings.value("deepseek_api_key", "")
        if not api_key:
            QMessageBox.information(self, "提示", "未配置 DeepSeek API Key。\n请在主菜单「设置」中配置。")
            return

        model = parent.settings.value("deepseek_model", "deepseek-chat")

        self.ai_btn.setEnabled(False)
        self.ai_btn.setText("生成中...")

        self.ai_worker = AiRemarkWorker(checked_files, api_key, model)
        self.ai_worker.finished.connect(self._on_ai_done)
        self.ai_worker.error.connect(self._on_ai_error)
        self.ai_worker.start()

    def _on_ai_done(self, text: str):
        self.msg_edit.setText(text)
        self.ai_btn.setEnabled(True)
        self.ai_btn.setText("\U0001f916 AI 生成")

    def _on_ai_error(self, err: str):
        QMessageBox.warning(self, "AI 生成失败", err)
        self.ai_btn.setEnabled(True)
        self.ai_btn.setText("\U0001f916 AI 生成")

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
        self.projects_registry = {}   # {project_name: {local_path, local_version, ...}}
        self.server_projects = []     # 服务端项目列表 [{name, latest_version, ...}]
        self._offline_cache = []      # 离线模式缓存的版本列表
        self._connected = False       # 心跳检测连接状态

        self._init_ui()
        self._restore_settings()
        self.setAcceptDrops(True)

        # 心跳定时器
        self.heartbeat_timer = QTimer(self)
        self.heartbeat_timer.timeout.connect(self._heartbeat_check)
        self.heartbeat_timer.start(30000)

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(6)

        # ---- 菜单栏 ----
        menubar = self.menuBar()
        help_menu = menubar.addMenu("帮助")
        help_menu.addAction("\u2699 设置").triggered.connect(self._open_settings)
        help_menu.addAction(f"\U0001f4e1 检查更新 (v{APP_VERSION})").triggered.connect(self._check_update)

        # ---- 服务器连接区 ----
        conn_group = QGroupBox("服务器连接")
        conn_layout = QHBoxLayout(conn_group)

        conn_layout.addWidget(QLabel("地址:"))
        self.addr_edit = QLineEdit()
        self.addr_edit.setPlaceholderText("192.168.1.x:5000")
        conn_layout.addWidget(self.addr_edit, 1)

        self.discover_btn = QPushButton("\U0001f50d 自动搜索")
        self.discover_btn.clicked.connect(self._discover_server)
        conn_layout.addWidget(self.discover_btn)

        self.connect_btn = QPushButton("连接")
        self.connect_btn.clicked.connect(self._connect_server)
        conn_layout.addWidget(self.connect_btn)

        self.conn_status = QLabel("未连接")
        self.conn_status.setStyleSheet("color: red;")
        conn_layout.addWidget(self.conn_status)

        main_layout.addWidget(conn_group)

        # ---- 服务端工程列表 ----
        proj_group = QGroupBox("服务端工程")
        proj_layout = QVBoxLayout(proj_group)
        proj_layout.setSpacing(4)

        self.proj_table = QTableWidget()
        self.proj_table.setColumnCount(5)
        self.proj_table.setHorizontalHeaderLabels(["工程名", "最新版本", "文件数", "最后提交", "本地状态"])
        self.proj_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.proj_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.proj_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.proj_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.proj_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.proj_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.proj_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.proj_table.setAlternatingRowColors(True)
        self.proj_table.setMinimumHeight(100)
        self.proj_table.setMaximumHeight(200)
        self.proj_table.itemSelectionChanged.connect(self._on_project_selected)
        self.proj_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.proj_table.customContextMenuRequested.connect(self._on_proj_menu)
        proj_layout.addWidget(self.proj_table)

        proj_btn_row = QHBoxLayout()
        self.refresh_proj_btn = QPushButton("\U0001f504 刷新项目列表")
        self.refresh_proj_btn.clicked.connect(self._refresh_projects)
        proj_btn_row.addWidget(self.refresh_proj_btn)

        self.deploy_btn = QPushButton("\U0001f4e6 部署到本地")
        self.deploy_btn.clicked.connect(self._deploy_project)
        proj_btn_row.addWidget(self.deploy_btn)

        self.sync_btn = QPushButton("\U0001f504 同步到最新")
        self.sync_btn.clicked.connect(self._sync_to_latest)
        proj_btn_row.addWidget(self.sync_btn)

        proj_layout.addLayout(proj_btn_row)
        main_layout.addWidget(proj_group)

        # ---- 当前工程信息 ----
        info_layout = QHBoxLayout()
        self.project_info = QLabel("未选择工程")
        self.project_info.setStyleSheet("font-weight: bold;")
        info_layout.addWidget(self.project_info)
        self.folder_info = QLabel("")
        info_layout.addWidget(self.folder_info, 1)
        self.select_folder_btn = QPushButton("浏览文件夹...")
        self.select_folder_btn.clicked.connect(self._browse_folder)
        info_layout.addWidget(self.select_folder_btn)
        self.drop_hint = QLabel("\U0001f4c2 或拖放文件夹到窗口")
        self.drop_hint.setStyleSheet("color: #888; font-size: 11px; padding: 2px 6px; border: 1px dashed #ccc; border-radius: 3px;")
        info_layout.addWidget(self.drop_hint)
        main_layout.addLayout(info_layout)

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
        self.version_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.version_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.version_table.setAlternatingRowColors(True)
        self.version_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.version_table.customContextMenuRequested.connect(self._on_version_menu)
        self.version_table.cellDoubleClicked.connect(self._on_version_double_clicked)
        list_layout.addWidget(self.version_table)

        self.refresh_btn = QPushButton("\U0001f504 刷新版本列表")
        self.refresh_btn.clicked.connect(self._refresh_versions)
        list_layout.addWidget(self.refresh_btn)

        main_layout.addWidget(list_group, 1)

        # ---- 操作按钮区 ----
        action_layout = QHBoxLayout()

        self.commit_btn = QPushButton("\U0001f4be 保存版本")
        self.commit_btn.setMinimumHeight(36)
        self.commit_btn.clicked.connect(self._commit)
        action_layout.addWidget(self.commit_btn)

        self.rollback_btn = QPushButton("\U0001f4e4 还原选中版本")
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
        has_project = bool(self.project_name)
        self.commit_btn.setEnabled(connected and has_folder and has_project)
        self.rollback_btn.setEnabled(connected and has_folder and has_project)
        self.refresh_btn.setEnabled(connected and has_project)
        self.refresh_proj_btn.setEnabled(connected)
        self.deploy_btn.setEnabled(connected)
        self.sync_btn.setEnabled(connected and has_folder and has_project)

    # ------------------------------------------------------------------
    # 本地工程注册表
    # ------------------------------------------------------------------
    def _reg_path(self) -> Path:
        return Path(__file__).parent.resolve() / ".mysvn_projects.json"

    def _save_registry(self):
        try:
            self._reg_path().write_text(
                json.dumps({"projects": self.projects_registry}, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass

    def _load_registry(self):
        try:
            if self._reg_path().exists():
                data = json.loads(self._reg_path().read_text(encoding="utf-8"))
                self.projects_registry = data.get("projects", {})
        except Exception:
            self.projects_registry = {}

    # ------------------------------------------------------------------
    # 设置持久化
    # ------------------------------------------------------------------
    def _restore_settings(self):
        last_addr = self.settings.value("last_server_address", "")
        if last_addr:
            self.addr_edit.setText(last_addr)
        self._load_registry()

    def _save_settings(self):
        self.settings.setValue("last_server_address", self.addr_edit.text().strip())
        self._save_registry()

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
            self.current_version = 0
            self.folder_info.setText(folder)
            self.project_name = os.path.basename(folder)
            # 清理本地 manifest
            manifest_p = Path(folder) / ".mysvn_manifest.json"
            if manifest_p.exists():
                try:
                    manifest_p.unlink(missing_ok=True)
                except Exception:
                    pass
            # 更新注册表
            self.projects_registry[self.project_name] = {
                "local_path": folder,
                "local_version": 0,
                "last_sync": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._save_settings()
            self._update_button_states()
            self._refresh_versions()
            self._refresh_projects()
            self.status_bar.showMessage(f"已选择: {self.project_name}  ({folder})")

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
        self.discover_btn.setText("\U0001f50d 自动搜索")
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

    # ------------------------------------------------------------------
    # 拖放支持
    # ------------------------------------------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            # 视觉反馈：边框发光效果
            self.setStyleSheet("QMainWindow { border: 3px solid #4CAF50; }")
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self.setStyleSheet("")

    def dropEvent(self, event):
        self.setStyleSheet("")
        urls = event.mimeData().urls()
        if urls:
            folder = urls[0].toLocalFile()
            if os.path.isdir(folder):
                self._on_folder_dropped(folder)

    def _on_folder_dropped(self, folder: str):
        self.local_folder = folder
        self.project_name = os.path.basename(folder)
        self.current_version = 0
        self.folder_info.setText(folder)
        self.project_info.setText(f"\U0001f4c1 {self.project_name}")
        # 清理本地 manifest（服务端版本可能已被删除）
        manifest_p = Path(folder) / ".mysvn_manifest.json"
        if manifest_p.exists():
            try:
                manifest_p.unlink(missing_ok=True)
            except Exception:
                pass
        # 更新注册表
        self.projects_registry[self.project_name] = {
            "local_path": folder,
            "local_version": 0,
            "last_sync": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._save_settings()
        self._update_button_states()
        self._refresh_versions()
        self._refresh_projects()
        self.status_bar.showMessage(f"已拖入工程: {self.project_name}")

    # ------------------------------------------------------------------
    # 心跳检测
    # ------------------------------------------------------------------
    def _heartbeat_check(self):
        if not self.server_url:
            return
        try:
            resp = requests.get(f"{self.server_url}/api/projects", timeout=3)
            if resp.ok:
                if not self._connected:
                    self._connected = True
                    self.conn_status.setText("已连接")
                    self.conn_status.setStyleSheet("color: green;")
                return
        except Exception:
            pass
        # 连接丢失
        if self._connected:
            self._connected = False
            self.conn_status.setText("连接断开")
            self.conn_status.setStyleSheet("color: red; font-weight: bold;")
            self.status_bar.showMessage("与服务器的连接已断开")
            self._update_button_states()

    def _connect_server(self):
        addr = self.addr_edit.text().strip()
        # 如果已连接，再次点击视为断开
        if self.server_url:
            self._disconnect_server()
            return
        if not addr:
            self.conn_status.setText("请输入地址")
            return
        if not addr.startswith("http"):
            addr = f"http://{addr}"
        self.server_url = addr
        self._connected = True
        self.conn_status.setText("已连接")
        self.conn_status.setStyleSheet("color: green;")
        self.connect_btn.setText("断开连接")
        self._save_settings()
        self._update_button_states()
        self.status_bar.showMessage(f"已连接到 {addr}")
        self._refresh_projects()

    def _disconnect_server(self):
        self.server_url = ""
        self._connected = False
        self._offline_cache = []
        self.conn_status.setText("未连接")
        self.conn_status.setStyleSheet("color: red;")
        self.connect_btn.setText("连接")
        self.server_projects = []
        self.proj_table.setRowCount(0)
        self.version_table.setRowCount(0)
        self._update_button_states()
        self.status_bar.showMessage("已断开连接")

    def _cache_path(self) -> Path:
        return Path(__file__).parent.resolve() / ".mysvn_cache.json"

    def _clear_offline_cache(self):
        try:
            if self._cache_path().exists():
                self._cache_path().unlink(missing_ok=True)
        except Exception:
            pass

    def _save_offline_cache(self, versions: list):
        try:
            data = {
                "project": self.project_name,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "timestamp": time.time(),
                "versions": versions,
            }
            self._cache_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _load_offline_cache(self) -> list:
        try:
            if self._cache_path().exists():
                data = json.loads(self._cache_path().read_text(encoding="utf-8"))
                # 缓存超过 1 小时视为过期，不加载
                cache_time = data.get("timestamp", 0)
                if time.time() - cache_time > 3600:
                    self._clear_offline_cache()
                    return []
                if data.get("project") == self.project_name:
                    return data.get("versions", [])
        except Exception:
            pass
        return []

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
        else:
            # 服务端已无版本（工程可能已被删除），重置版本号
            self.current_version = 0
        self.status_bar.showMessage(f"已加载 {len(versions)} 个版本")
        # 缓存到本地（离线模式）
        self._offline_cache = versions
        if versions:
            self._save_offline_cache(versions)
        else:
            # 空版本列表时清理离线缓存，避免后续误加载旧数据
            self._clear_offline_cache()

    def _on_versions_error(self, msg: str):
        # 尝试加载离线缓存
        cached = self._load_offline_cache()
        if cached:
            self._on_versions_loaded(cached)
            self.status_bar.showMessage(f"离线模式 — 已加载缓存版本（{len(cached)} 个版本）")
            self.conn_status.setText("离线模式")
            self.conn_status.setStyleSheet("color: orange;")
        else:
            self.status_bar.showMessage(f"获取版本失败: {msg}")

    # ------------------------------------------------------------------
    # 服务端工程列表
    # ------------------------------------------------------------------
    def _refresh_projects(self):
        if not self.server_url:
            return
        self.status_bar.showMessage("正在获取服务端工程列表...")
        self.proj_worker = FetchProjectsWorker(self.server_url)
        self.proj_worker.finished.connect(self._on_projects_loaded)
        self.proj_worker.error.connect(self._on_projects_error)
        self.proj_worker.start()

    def _on_projects_loaded(self, projects: list):
        self.server_projects = projects
        self.proj_table.setRowCount(len(projects))
        for i, p in enumerate(projects):
            name = p["name"]
            self.proj_table.setItem(i, 0, QTableWidgetItem(name))
            self.proj_table.setItem(i, 1, QTableWidgetItem(f"v{p['latest_version']}"))
            self.proj_table.setItem(i, 2, QTableWidgetItem(str(p["file_count"])))
            self.proj_table.setItem(i, 3, QTableWidgetItem(p.get("last_commit_time", "")))

            # 本地状态
            reg = self.projects_registry.get(name, {})
            local_path = reg.get("local_path", "")
            local_ver = reg.get("local_version", 0)
            if local_path and os.path.isdir(local_path):
                if local_ver >= p["latest_version"]:
                    status = f"\u2713 已部署 v{local_ver}"
                else:
                    status = f"\u2191 落后 (本地v{local_ver})"
            else:
                status = "未部署"
            self.proj_table.setItem(i, 4, QTableWidgetItem(status))
        self.status_bar.showMessage(f"共 {len(projects)} 个项目")

        # 自动恢复上次使用的工程
        if not self.local_folder:
            for name, reg in self.projects_registry.items():
                lp = reg.get("local_path", "")
                if lp and os.path.isdir(lp):
                    # 检查服务端有没有同名工程
                    matched = [p for p in projects if p["name"] == name]
                    if matched:
                        self.local_folder = lp
                        self.project_name = name
                        self.current_version = reg.get("local_version", 0)
                        self.folder_info.setText(lp)
                        self.project_info.setText(f"\U0001f4c1 {name}")
                        self._update_button_states()
                        self._refresh_versions()
                        self.status_bar.showMessage(f"已恢复工程: {name}")
                        break

    def _on_projects_error(self, msg: str):
        self.status_bar.showMessage(f"获取项目列表失败: {msg}")

    def _on_project_selected(self):
        rows = self.proj_table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        proj_name = self.proj_table.item(row, 0).text()
        latest = int(self.proj_table.item(row, 1).text().lstrip("v"))

        # 检查注册表中是否已关联本地文件夹
        reg = self.projects_registry.get(proj_name, {})
        local_path = reg.get("local_path", "")
        if local_path and os.path.isdir(local_path):
            self.local_folder = local_path
            self.project_name = proj_name
            self.current_version = reg.get("local_version", 0)
            self.folder_info.setText(local_path)
            self.project_info.setText(f"\U0001f4c1 {proj_name}")
            self._update_button_states()
            self._refresh_versions()
            self.status_bar.showMessage(f"已切换到工程: {proj_name}")
        else:
            self.local_folder = ""
            self.project_name = proj_name
            self.current_version = 0
            self.project_info.setText(f"\U0001f4c1 {proj_name} (未部署)")
            self.folder_info.setText("点击「部署到本地」下载")
            self._update_button_states()

    # ------------------------------------------------------------------
    # 工程表右键菜单
    # ------------------------------------------------------------------
    def _on_proj_menu(self, pos):
        item = self.proj_table.itemAt(pos)
        if not item:
            return
        row = item.row()
        proj_name = self.proj_table.item(row, 0).text()

        menu = QMenu(self)
        action_deploy = QAction("部署到本地", self)
        action_deploy.triggered.connect(self._deploy_project)
        menu.addAction(action_deploy)

        action_sync = QAction("同步到最新", self)
        action_sync.triggered.connect(self._sync_to_latest)
        menu.addAction(action_sync)

        action_rename = QAction("重命名工程...", self)
        action_rename.triggered.connect(lambda: self._rename_project(proj_name))
        menu.addAction(action_rename)

        menu.addSeparator()

        # 打开本地路径（如果已部署）
        reg = self.projects_registry.get(proj_name, {})
        local_path = reg.get("local_path", "")
        if local_path and os.path.isdir(local_path):
            action_open = QAction("在资源管理器中打开", self)
            action_open.triggered.connect(lambda: os.startfile(local_path))
            menu.addAction(action_open)
            menu.addSeparator()

        action_delete = QAction("删除工程", self)
        action_delete.triggered.connect(lambda: self._delete_project(proj_name))
        action_delete.setIcon(self.style().standardIcon(QStyle.SP_TrashIcon))
        menu.addAction(action_delete)

        menu.exec_(self.proj_table.viewport().mapToGlobal(pos))

    def _rename_project(self, proj_name: str):
        new_name, ok = QInputDialog.getText(
            self, "重命名工程",
            f"将「{proj_name}」重命名为:",
            text=proj_name,
        )
        if not ok or not new_name.strip() or new_name.strip() == proj_name:
            return
        new_name = new_name.strip()

        self.status_bar.showMessage(f"正在重命名工程...")
        sv_url = self.server_url

        class RenameWorker(QThread):
            result = pyqtSignal(dict)

            def run(self):
                try:
                    resp = requests.post(
                        f"{sv_url}/api/rename_project",
                        json={"old_name": proj_name, "new_name": new_name},
                        timeout=HTTP_TIMEOUT,
                    )
                    resp.raise_for_status()
                    self.result.emit(resp.json())
                except Exception as e:
                    self.result.emit({"error": str(e)})

        self.rename_worker = RenameWorker()
        self.rename_worker.result.connect(lambda r: self._on_rename_result(r, proj_name, new_name))
        self.rename_worker.start()

    def _on_rename_result(self, result: dict, old_name: str, new_name: str):
        if result.get("error"):
            self.status_bar.showMessage("重命名失败")
            QMessageBox.critical(self, "重命名失败", result["error"])
            return

        # 更新注册表
        if old_name in self.projects_registry:
            self.projects_registry[new_name] = self.projects_registry.pop(old_name)
            self._save_settings()

        # 如果当前工程就是这个，也更新
        if self.project_name == old_name:
            self.project_name = new_name
            self.project_info.setText(f"\U0001f4c1 {new_name}")

        self.status_bar.showMessage(f"工程已重命名: {old_name} → {new_name}")
        self._refresh_projects()

    def _delete_project(self, proj_name: str):
        reply = QMessageBox.question(
            self, "确认删除工程",
            f"确定要永久删除工程「{proj_name}」及其所有版本吗？\n\n"
            "此操作将从服务器上移除所有相关数据，不可撤销！",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self.status_bar.showMessage(f"正在删除工程 {proj_name}...")
        sv_url = self.server_url

        class DeleteProjectWorker(QThread):
            result = pyqtSignal(dict)

            def run(self):
                try:
                    resp = requests.post(
                        f"{sv_url}/api/delete_project/{proj_name}",
                        timeout=HTTP_TIMEOUT,
                    )
                    resp.raise_for_status()
                    self.result.emit(resp.json())
                except Exception as e:
                    self.result.emit({"error": str(e)})

        self.del_proj_worker = DeleteProjectWorker()
        self.del_proj_worker.result.connect(lambda r: self._on_delete_project_result(r, proj_name))
        self.del_proj_worker.start()

    def _on_delete_project_result(self, result: dict, proj_name: str):
        if result.get("error"):
            self.status_bar.showMessage("删除失败")
            QMessageBox.critical(self, "删除失败", result["error"])
            return

        # 从注册表中移除
        reg = self.projects_registry.pop(proj_name, None)
        self._save_settings()

        # 清理本地 manifest（如果本地有对应文件夹）
        if reg:
            local_path = reg.get("local_path", "")
            if local_path:
                mp = Path(local_path) / ".mysvn_manifest.json"
                if mp.exists():
                    try:
                        mp.unlink(missing_ok=True)
                    except Exception:
                        pass

        # 如果当前选中的就是这个工程，清除状态
        if self.project_name == proj_name:
            self.local_folder = ""
            self.project_name = ""
            self.current_version = 0
            self.folder_info.setText("")
            self.project_info.setText("未选择工程")
            self.version_table.setRowCount(0)

        self.status_bar.showMessage(f"工程 {proj_name} 已删除（共 {result.get('deleted_versions', 0)} 个版本）")
        QMessageBox.information(self, "删除成功",
            f"工程「{proj_name}」已从服务器永久删除。\n"
            f"共清理 {result.get('deleted_versions', 0)} 个版本。")
        self._refresh_projects()

    # ------------------------------------------------------------------
    # 部署到本地
    # ------------------------------------------------------------------
    def _deploy_project(self):
        rows = self.proj_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.warning(self, "提示", "请先在工程列表中选择一个项目")
            return
        row = rows[0].row()
        proj_name = self.proj_table.item(row, 0).text()
        latest_ver = int(self.proj_table.item(row, 1).text().lstrip("v"))

        folder = QFileDialog.getExistingDirectory(self, f"选择存放 {proj_name} 的文件夹")
        if not folder:
            return

        target = Path(folder)
        if target.exists() and any(target.iterdir()):
            reply = QMessageBox.question(self, "文件夹不为空",
                f"文件夹 '{folder}' 不为空，确定要写入吗？\n同名文件将被覆盖。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return

        self.local_folder = str(target)
        self.project_name = proj_name
        self.folder_info.setText(str(target))
        self.project_info.setText(f"\U0001f4c1 {proj_name}")

        self.deploy_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(0)
        self.status_bar.showMessage(f"正在部署 {proj_name} v{latest_ver}...")

        sv_url = self.server_url
        ver = latest_ver

        class DeployWorker(QThread):
            progress = pyqtSignal(str)
            done = pyqtSignal()
            error = pyqtSignal(str)

            def run(self_):
                try:
                    self_.progress.emit("正在下载项目文件...")
                    resp = requests.get(
                        f"{sv_url}/api/download/{ver}",
                        timeout=UPLOAD_TIMEOUT,
                    )
                    resp.raise_for_status()
                    self_.progress.emit("正在解压到本地文件夹...")
                    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                        zf.extractall(str(target))
                    self_.done.emit()
                except requests.ConnectionError:
                    self_.error.emit("无法连接到服务器")
                except requests.Timeout:
                    self_.error.emit("下载超时，请重试")
                except zipfile.BadZipFile:
                    self_.error.emit("下载的版本文件已损坏")
                except Exception as e:
                    self_.error.emit(f"部署失败: {str(e)}")

        self.dpl_worker = DeployWorker()
        self.dpl_worker.progress.connect(self._on_deploy_progress)
        self.dpl_worker.done.connect(lambda: self._on_deploy_done(proj_name, ver, str(target)))
        self.dpl_worker.error.connect(self._on_deploy_error)
        self.dpl_worker.start()

    def _on_deploy_progress(self, msg: str):
        self.status_bar.showMessage(msg)

    def _on_deploy_done(self, proj_name: str, ver: int, path: str):
        self.deploy_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.status_bar.showMessage(f"部署完成: {proj_name} v{ver}")

        # 更新注册表
        self.projects_registry[proj_name] = {
            "local_path": path,
            "local_version": ver,
            "last_sync": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._save_settings()
        self._refresh_projects()
        self._refresh_versions()
        QMessageBox.information(self, "部署成功",
            f"{proj_name} v{ver} 已部署到:\n{path}")

    def _on_deploy_error(self, msg: str):
        self.deploy_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.status_bar.showMessage("部署失败")
        QMessageBox.critical(self, "部署失败", msg)

    # ------------------------------------------------------------------
    # 同步到最新版本（增量）
    # ------------------------------------------------------------------
    def _sync_to_latest(self):
        if not self.local_folder:
            QMessageBox.warning(self, "提示", "请先选择本地工程文件夹")
            return
        if not self.project_name:
            QMessageBox.warning(self, "提示", "未选择工程")
            return

        # 找服务端最新版本
        latest_ver = 0
        for p in self.server_projects:
            if p["name"] == self.project_name:
                latest_ver = p["latest_version"]
                break

        if latest_ver == 0:
            QMessageBox.warning(self, "提示", "服务端未找到此工程")
            return

        reg = self.projects_registry.get(self.project_name, {})
        local_ver = reg.get("local_version", 0)
        if local_ver >= latest_ver:
            QMessageBox.information(self, "已最新",
                f"本地已是 v{local_ver}，服务端最新 v{latest_ver}，无需同步。")
            return

        self.status_bar.showMessage(f"正在同步至 v{latest_ver}...")
        self._do_rollback(latest_ver)

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
        total_bytes = sum(file_sizes.get(f, 0) for f in manifest)
        self.status_bar.showMessage(
            f"扫描完成: {len(manifest)} 个文件, 合计 {_format_size(total_bytes)} — 正在对比服务端...")
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
        need_upload = result.get("need_upload", [])

        if conflicts:
            file_list = "\n".join(f"  - {f}" for f in conflicts[:20])
            extra = f"\n  ...还有 {len(conflicts)-20} 个冲突文件" if len(conflicts) > 20 else ""
            self.status_bar.showMessage("检测到冲突")

            dlg = ConflictDialog(conflicts, self)
            choice = dlg.exec_()
            if choice == QDialog.Accepted:
                # 用户选择强制覆盖
                if not need_upload:
                    QMessageBox.information(self, "提示", "没有检测到任何变更")
                    return
                dlg2 = CommitFileDialog(self._pending_file_sizes, need_upload, self)
                if dlg2.exec_() == QDialog.Accepted and dlg2.confirmed:
                    self._do_upload(self._pending_manifest, dlg2.message, dlg2.selected_files, force=True)
            # 用户取消，什么也不做
            return

        if not need_upload:
            self.status_bar.showMessage("没有变更")
            QMessageBox.information(self, "提示", "没有检测到任何变更")
            return

        dlg = CommitFileDialog(self._pending_file_sizes, need_upload, self)
        if dlg.exec_() == QDialog.Accepted and dlg.confirmed:
            self._do_upload(self._pending_manifest, dlg.message, dlg.selected_files)

    def _do_upload(self, manifest: dict, message: str, selected_files: list = None, force: bool = False):
        self.commit_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(0)
        self.status_bar.showMessage("正在上传...")

        self.upload_worker = UploadWorker(
            self.server_url, self.local_folder, manifest,
            self.username, message, self.current_version,
            self.project_name, selected_files, force,
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
        # 更新注册表
        if self.project_name:
            self.projects_registry[self.project_name] = {
                "local_path": self.local_folder,
                "local_version": result["version_id"],
                "last_sync": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._save_settings()
            self._refresh_projects()
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
    # 检查更新（基于 Git commit 对比）
    # ------------------------------------------------------------------
    def _check_update(self):
        self.status_bar.showMessage("正在检查更新...")
        self.update_worker = UpdateCheckWorker()
        self.update_worker.finished.connect(self._on_update_result)
        self.update_worker.error.connect(self._on_update_error)
        self.update_worker.start()

    def _on_update_result(self, info: dict):
        source = info.get("source", "")
        if not info.get("has_update"):
            QMessageBox.information(self, "检查更新",
                f"当前已是最新版本\n本地: {info.get('local_sha', '')}\n来源: {source}")
            self.status_bar.showMessage("已是最新版本")
            return

        local_sha = info.get("local_sha", "?")
        remote_sha = info.get("remote_sha", "?")
        commit_msg = info.get("commit_msg", "")

        msg = (
            f"发现新提交！\n\n"
            f"本地版本: {local_sha}\n"
            f"远程最新: {remote_sha}\n"
            f"最新提交: {commit_msg}\n\n"
            "是否立即执行 git pull 更新？"
        )
        result = QMessageBox.question(
            self, "发现更新", msg,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes
        )
        if result == QMessageBox.Yes:
            self._do_git_pull()

    def _do_git_pull(self):
        """后台执行 git pull 一键更新"""
        self.status_bar.showMessage("正在 git pull...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(0)

        class GitPullWorker(QThread):
            done = pyqtSignal(bool, str)

            def run(self):
                try:
                    result = subprocess.run(
                        ["git", "pull", "origin", "main"],
                        cwd=GIT_REPO_PATH,
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    success = result.returncode == 0
                    output = result.stdout.strip() or result.stderr.strip()
                    if not output:
                        output = "Already up to date." if success else "更新失败"
                    self.done.emit(success, output)
                except subprocess.TimeoutExpired:
                    self.done.emit(False, "git pull 超时，请手动执行")
                except FileNotFoundError:
                    self.done.emit(False, "未找到 git 命令，请确保 Git 已安装")
                except Exception as e:
                    self.done.emit(False, str(e))

        self.git_worker = GitPullWorker()
        self.git_worker.done.connect(self._on_git_pull_done)
        self.git_worker.start()

    def _on_git_pull_done(self, success: bool, output: str):
        self.progress_bar.setVisible(False)
        if success:
            QMessageBox.information(self, "更新完成",
                f"代码已更新到最新版本。\n\n{output}\n\n请重启客户端以加载新代码。")
            self.status_bar.showMessage("更新完成，请重启客户端")
        else:
            QMessageBox.critical(self, "更新失败",
                f"git pull 执行失败：\n\n{output}\n\n请手动在命令行执行 git pull。")
            self.status_bar.showMessage("更新失败")

    def _on_update_error(self, err: str):
        self.status_bar.showMessage("检查更新失败")
        QMessageBox.warning(self, "检查更新失败",
            f"无法检查更新：{err}\n\n"
            "请稍后重试，或手动访问 GitHub 仓库查看最新版本。")

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
        # 更新注册表版本和本地 manifest
        if self.project_name:
            ver_id = self.rollback_worker.version_id if hasattr(self, 'rollback_worker') else 0
            if ver_id:
                self.projects_registry[self.project_name] = {
                    "local_path": self.local_folder,
                    "local_version": ver_id,
                    "last_sync": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                self._save_settings()
                self._refresh_projects()
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
    def _on_version_double_clicked(self, row: int, column: int):
        """双击版本行查看文件列表"""
        version_id = int(self.version_table.item(row, 0).text())
        version_msg = self.version_table.item(row, 3).text()
        self._show_version_files(version_id, version_msg)

    def _on_version_menu(self, pos):
        selected_rows = set()
        for item in self.version_table.selectedItems():
            selected_rows.add(item.row())
        item = self.version_table.itemAt(pos)
        if not item:
            return

        # 如果右键的行不在已选列表中，选中它
        click_row = item.row()
        if click_row not in selected_rows:
            self.version_table.selectRow(click_row)
            selected_rows = {click_row}

        version_ids = []
        for r in selected_rows:
            vid = int(self.version_table.item(r, 0).text())
            version_ids.append(vid)
        version_ids.sort(reverse=True)

        primary_vid = version_ids[0]
        primary_msg = self.version_table.item(click_row, 3).text()

        menu = QMenu(self)

        if len(version_ids) == 1:
            vid = primary_vid
            action_restore = QAction("还原到当前文件夹", self)
            action_restore.triggered.connect(lambda _a=None: self._rollback_version(vid))
            menu.addAction(action_restore)

            action_download = QAction("下载到指定目录...", self)
            action_download.triggered.connect(lambda _a=None: self._download_version(vid))
            menu.addAction(action_download)

            menu.addSeparator()

            action_files = QAction("查看文件列表", self)
            action_files.triggered.connect(lambda _a=None, _m=primary_msg: self._show_version_files(vid, _m))
            menu.addAction(action_files)

            # 如果表中还有其它版本，提供与上一版本的对比
            all_ids = []
            for r in range(self.version_table.rowCount()):
                all_ids.append(int(self.version_table.item(r, 0).text()))
            all_ids.sort(reverse=True)
            prev_vid = None
            for a in all_ids:
                if a < vid:
                    prev_vid = a
                    break
            if prev_vid is not None:
                action_diff = QAction(f"对比 v{vid} 与 v{prev_vid}", self)
                action_diff.triggered.connect(
                    lambda _a=None, o=prev_vid, n=vid: self._diff_versions(o, n))
                menu.addAction(action_diff)

            menu.addSeparator()

        else:
            # 多选状态
            action_batch_del = QAction(f"批量删除 {len(version_ids)} 个版本", self)
            action_batch_del.triggered.connect(
                lambda _a=None, ids=version_ids: self._batch_delete_versions(ids))
            action_batch_del.setIcon(self.style().standardIcon(QStyle.SP_TrashIcon))
            menu.addAction(action_batch_del)
            menu.addSeparator()

        action_delete = QAction("删除版本（当前选中）" if len(version_ids) > 1 else "删除版本", self)
        action_delete.triggered.connect(lambda _a=None: self._delete_version(primary_vid))
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

    # ------------------------------------------------------------------
    # 批量删除
    # ------------------------------------------------------------------
    def _batch_delete_versions(self, version_ids: list):
        reply = QMessageBox.question(
            self, "确认批量删除",
            f"确定要永久删除 {len(version_ids)} 个版本吗？\n\n"
            f"版本: v{', v'.join(str(v) for v in version_ids)}\n\n此操作不可撤销！",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self.status_bar.showMessage(f"正在批量删除 {len(version_ids)} 个版本...")

        sv_url = self.server_url

        class BatchDelWorker(QThread):
            progress = pyqtSignal(int, int)
            done = pyqtSignal(int)
            error = pyqtSignal(str)

            def run(self):
                for i, vid in enumerate(version_ids):
                    try:
                        resp = requests.post(
                            f"{sv_url}/api/delete_version/{vid}",
                            timeout=HTTP_TIMEOUT,
                        )
                        resp.raise_for_status()
                    except Exception:
                        pass
                    self.progress.emit(i + 1, len(version_ids))
                self.done.emit(len(version_ids))

        self.batch_del_worker = BatchDelWorker()
        self.batch_del_worker.progress.connect(
            lambda c, t: self.status_bar.showMessage(f"批量删除: {c}/{t}")
        )
        self.batch_del_worker.done.connect(lambda n: self._on_batch_delete_done(n))
        self.batch_del_worker.start()

    def _on_batch_delete_done(self, count: int):
        self.status_bar.showMessage(f"已删除 {count} 个版本")
        QMessageBox.information(self, "批量删除完成", f"已成功删除 {count} 个版本。")
        self._refresh_versions()

    # ------------------------------------------------------------------
    # 版本差异对比
    # ------------------------------------------------------------------
    def _diff_versions(self, older_vid: int, newer_vid: int):
        self.status_bar.showMessage(f"正在获取 v{older_vid} 和 v{newer_vid} 的文件列表...")
        sv_url = self.server_url

        class DiffFetchWorker(QThread):
            result = pyqtSignal(list, list)
            error = pyqtSignal(str)

            def run(self):
                try:
                    resp1 = requests.get(
                        f"{sv_url}/api/version_files/{older_vid}", timeout=HTTP_TIMEOUT)
                    resp2 = requests.get(
                        f"{sv_url}/api/version_files/{newer_vid}", timeout=HTTP_TIMEOUT)
                    resp1.raise_for_status()
                    resp2.raise_for_status()
                    f1 = resp1.json().get("files", [])
                    f2 = resp2.json().get("files", [])
                    self.result.emit(f1, f2)
                except Exception as e:
                    self.error.emit(str(e))

        self.diff_worker = DiffFetchWorker()
        self.diff_worker.result.connect(
            lambda f1, f2: self._show_diff_dialog(older_vid, newer_vid, f1, f2)
        )
        self.diff_worker.error.connect(lambda e: QMessageBox.critical(self, "获取失败", e))
        self.diff_worker.start()

    def _show_diff_dialog(self, older_vid: int, newer_vid: int, files_old: list, files_new: list):
        old_map = {f["path"]: f["md5"] for f in files_old}
        new_map = {f["path"]: f["md5"] for f in files_new}

        added = [p for p in new_map if p not in old_map]
        removed = [p for p in old_map if p not in new_map]
        modified = [p for p in old_map if p in new_map and old_map[p] != new_map[p]]

        dlg = QDialog(self)
        dlg.setWindowTitle(f"版本差异对比 v{older_vid} → v{newer_vid}")
        dlg.resize(600, 450)
        layout = QVBoxLayout(dlg)

        layout.addWidget(QLabel(
            f"<b>差异摘要</b> — 新增: {len(added)}  删除: {len(removed)}  修改: {len(modified)}"))

        tree = QTreeWidget()
        tree.setHeaderLabels(["文件路径", "状态"])
        tree.setAlternatingRowColors(True)

        for p in added:
            item = QTreeWidgetItem(tree, [p, "新增"])
            item.setForeground(1, QBrush(QColor("green")))
        for p in removed:
            item = QTreeWidgetItem(tree, [p, "删除"])
            item.setForeground(1, QBrush(QColor("red")))
        for p in modified:
            item = QTreeWidgetItem(tree, [p, "修改"])
            item.setForeground(1, QBrush(QColor("orange")))

        layout.addWidget(tree, 1)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn)

        dlg.exec_()


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
