#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
My-SVN 服务端
基于 Flask, SQLite, UDP 广播
双击运行即可，无需任何配置
"""

import os
import sys
import json
import hashlib
import sqlite3
import socket
import threading
import time
import shutil
import logging
import zipfile
import io
from pathlib import Path
from datetime import datetime

from flask import Flask, request, jsonify, send_file

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("MySVN-Server")

# ---------------------------------------------------------------------------
# 全局配置
# ---------------------------------------------------------------------------
SRC_DIR = Path(__file__).parent.resolve()

# 数据目录优先级: 1) 命令行 --data-dir  2) 同级 data_dir.txt  3) %APPDATA%\MySVN\server
_data_dir = None
_data_from = "default"

# 1) 命令行参数
for arg in sys.argv[1:]:
    if arg.startswith("--data-dir="):
        _data_dir = arg.split("=", 1)[1]
        _data_from = "argv"
        break

# 2) 同级目录下的配置文件
if not _data_dir:
    cfg_file = SRC_DIR / "data_dir.txt"
    if cfg_file.exists():
        line = cfg_file.read_text(encoding="utf-8").strip()
        if line:
            _data_dir = line
            _data_from = "data_dir.txt"

# 3) 默认用户数据目录
if not _data_dir:
    _data_dir = os.path.join(os.environ.get("APPDATA", ""), "MySVN", "server")
    _data_from = "default"

DATA_DIR = Path(_data_dir).resolve()

REPO_DIR = DATA_DIR / "repo"
VERSIONS_DIR = REPO_DIR / "versions"
DB_PATH = REPO_DIR / "my_svn.db"
MANIFEST_DIR = REPO_DIR / "manifests"
TMP_DIR = DATA_DIR / "tmp_upload"

START_PORT = 5000
MAX_PORT_RETRY = 10
UDP_BROADCAST_PORT = 9999
BROADCAST_INTERVAL = 2


# ---------------------------------------------------------------------------
# 初始化目录和数据库
# ---------------------------------------------------------------------------
def ensure_dirs():
    for d in [REPO_DIR, VERSIONS_DIR, MANIFEST_DIR, TMP_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def get_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db: sqlite3.Connection):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS versions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT   NOT NULL DEFAULT 'default',
            username    TEXT    NOT NULL,
            commit_time TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            message     TEXT    NOT NULL DEFAULT '',
            base_version INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS file_records (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id  INTEGER NOT NULL,
            file_path   TEXT    NOT NULL,
            md5         TEXT    NOT NULL,
            file_size   INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (version_id) REFERENCES versions(id)
        );

        CREATE INDEX IF NOT EXISTS idx_file_version ON file_records(version_id);
        CREATE INDEX IF NOT EXISTS idx_file_path   ON file_records(file_path);
    """)
    # 兼容旧数据库：添加 project_name 列
    try:
        db.execute("ALTER TABLE versions ADD COLUMN project_name TEXT NOT NULL DEFAULT 'default'")
    except sqlite3.OperationalError:
        pass
    db.commit()


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def md5_of_file(filepath: Path) -> str:
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def get_latest_version_id(db: sqlite3.Connection, project_name: str = "default") -> int:
    row = db.execute("SELECT MAX(id) FROM versions WHERE project_name = ?", (project_name,)).fetchone()
    return row[0] if row[0] else 0


def get_file_manifest(version_id: int, db: sqlite3.Connection) -> dict:
    rows = db.execute(
        "SELECT file_path, md5 FROM file_records WHERE version_id = ?", (version_id,)
    ).fetchall()
    return {r["file_path"]: r["md5"] for r in rows}


def version_files_dir(version_id: int) -> Path:
    return VERSIONS_DIR / f"v{version_id}" / "files"


# ---------------------------------------------------------------------------
# Flask 应用
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response


@app.route("/api/check_files", methods=["POST"])
def check_files():
    """
    客户端上传本地文件清单 {filepath: md5}
    服务端与最新版本对比，返回需要上传的文件列表
    """
    try:
        data: dict = request.get_json(force=True)
        client_manifest: dict = data.get("manifest", {})
        base_version: int = data.get("base_version", 0)
        project_name: str = data.get("project_name", "default")

        db = get_db()
        latest_id = get_latest_version_id(db, project_name)

        if latest_id == 0:
            return jsonify({"need_upload": list(client_manifest.keys()), "latest_version": 0})

        server_manifest = get_file_manifest(latest_id, db)

        need_upload = []
        for rel_path, client_md5 in client_manifest.items():
            server_md5 = server_manifest.get(rel_path)
            if server_md5 is None or server_md5 != client_md5:
                need_upload.append(rel_path)

        # 乐观锁冲突检测
        conflicts = []
        if base_version > 0 and base_version < latest_id:
            base_manifest = get_file_manifest(base_version, db)
            for rel_path in client_manifest:
                base_md5 = base_manifest.get(rel_path)
                server_md5 = server_manifest.get(rel_path)
                client_md5 = client_manifest.get(rel_path, "")
                if base_md5 is not None and server_md5 is not None:
                    if base_md5 != server_md5 and server_md5 != client_md5:
                        conflicts.append(rel_path)
                elif base_md5 is None and server_md5 is not None:
                    conflicts.append(rel_path)

        db.close()
        return jsonify({
            "need_upload": need_upload,
            "latest_version": latest_id,
            "conflicts": conflicts,
        })
    except Exception as e:
        log.exception("check_files出错")
        return jsonify({"error": str(e)}), 500


@app.route("/api/upload", methods=["POST"])
def upload_files():
    """接收客户端上传的ZIP压缩包（仅包含变更文件）"""
    try:
        if "file" not in request.files:
            return jsonify({"error": "未收到文件"}), 400

        file = request.files["file"]
        username = request.form.get("username", "anonymous")
        message = request.form.get("message", "")
        base_version = int(request.form.get("base_version", 0))
        project_name = request.form.get("project_name", "default")

        tmp_dir = TMP_DIR / f"{username}_{int(time.time())}"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        try:
            with zipfile.ZipFile(io.BytesIO(file.read())) as zf:
                zf.extractall(str(tmp_dir))
        except zipfile.BadZipFile:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return jsonify({"error": "上传的文件已损坏"}), 400

        db = get_db()
        db.execute(
            "INSERT INTO versions (project_name, username, message, base_version) VALUES (?, ?, ?, ?)",
            (project_name, username, message, base_version),
        )
        db.commit()
        new_version_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        new_files_dir = version_files_dir(new_version_id)
        new_files_dir.mkdir(parents=True, exist_ok=True)

        # 从同一项目的上一版本复制所有文件（增量快照机制）
        prev_row = db.execute(
            "SELECT MAX(id) FROM versions WHERE project_name=? AND id < ?",
            (project_name, new_version_id),
        ).fetchone()
        if prev_row and prev_row[0]:
            prev_files_dir = version_files_dir(prev_row[0])
            if prev_files_dir.exists():
                shutil.copytree(str(prev_files_dir), str(new_files_dir), dirs_exist_ok=True)

        # 覆盖变更文件
        manifest = {}
        for fpath in tmp_dir.rglob("*"):
            if fpath.is_file():
                rel = fpath.relative_to(tmp_dir)
                rel_str = str(rel).replace("\\", "/")
                dest = new_files_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(fpath), str(dest))
                md5_val = md5_of_file(dest)
                size = dest.stat().st_size
                manifest[rel_str] = md5_val
                db.execute(
                    "INSERT INTO file_records (version_id, file_path, md5, file_size) VALUES (?, ?, ?, ?)",
                    (new_version_id, rel_str, md5_val, size),
                )

        # 同时记录从上一版本继承的未变更文件
        existing = set(manifest.keys())
        for fpath in new_files_dir.rglob("*"):
            if fpath.is_file():
                rel = fpath.relative_to(new_files_dir)
                rel_str = str(rel).replace("\\", "/")
                if rel_str not in existing:
                    md5_val = md5_of_file(fpath)
                    size = fpath.stat().st_size
                    manifest[rel_str] = md5_val
                    db.execute(
                        "INSERT INTO file_records (version_id, file_path, md5, file_size) VALUES (?, ?, ?, ?)",
                        (new_version_id, rel_str, md5_val, size),
                    )

        db.commit()
        shutil.rmtree(tmp_dir, ignore_errors=True)

        manifest_path = MANIFEST_DIR / f"v{new_version_id}.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        log.info(f"版本 {new_version_id} 已创建，用户: {username}，文件数: {len(manifest)}")
        db.close()
        return jsonify({
            "success": True,
            "version_id": new_version_id,
            "file_count": len(manifest),
            "manifest": manifest,
        })
    except Exception as e:
        log.exception("upload_files出错")
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects", methods=["GET"])
def list_projects():
    """返回所有项目及其最新版本信息"""
    try:
        db = get_db()
        rows = db.execute("""
            SELECT v.project_name,
                   MAX(v.id) as latest_version,
                   COUNT(f.id) as file_count,
                   v2.commit_time as last_commit_time
            FROM versions v
            LEFT JOIN file_records f ON f.version_id = v.id
            LEFT JOIN versions v2 ON v2.id = (SELECT MAX(id) FROM versions WHERE project_name = v.project_name)
            GROUP BY v.project_name
            ORDER BY v.project_name
        """).fetchall()
        projects = []
        for r in rows:
            projects.append({
                "name": r["project_name"],
                "latest_version": r["latest_version"],
                "file_count": r["file_count"],
                "last_commit_time": r["last_commit_time"] or "",
            })
        db.close()
        return jsonify({"projects": projects})
    except Exception as e:
        log.exception("list_projects出错")
        return jsonify({"error": str(e)}), 500


@app.route("/api/versions", methods=["GET"])
def list_versions():
    try:
        project_name = request.args.get("project", "default")
        db = get_db()
        rows = db.execute(
            "SELECT id, username, commit_time, message FROM versions WHERE project_name = ? ORDER BY id DESC LIMIT 50",
            (project_name,)
        ).fetchall()
        versions = [
            {
                "id": r["id"],
                "username": r["username"],
                "commit_time": r["commit_time"],
                "message": r["message"],
            }
            for r in rows
        ]
        db.close()
        return jsonify({"versions": versions})
    except Exception as e:
        log.exception("list_versions出错")
        return jsonify({"error": str(e)}), 500


@app.route("/api/version_files/<int:version_id>", methods=["GET"])
def list_version_files(version_id: int):
    """返回指定版本的文件列表及大小"""
    try:
        db = get_db()
        rows = db.execute(
            "SELECT file_path, md5, file_size FROM file_records WHERE version_id = ? ORDER BY file_path",
            (version_id,)
        ).fetchall()
        db.close()
        files = [
            {"path": r["file_path"], "md5": r["md5"], "size": r["file_size"]}
            for r in rows
        ]
        return jsonify({"files": files})
    except Exception as e:
        log.exception("list_version_files出错")
        return jsonify({"error": str(e)}), 500


@app.route("/api/download/<int:version_id>", methods=["GET"])
def download_version(version_id: int):
    try:
        files_dir = version_files_dir(version_id)
        if not files_dir.exists() or not any(files_dir.iterdir()):
            return jsonify({"error": f"版本 {version_id} 不存在或无文件"}), 404

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for fpath in files_dir.rglob("*"):
                if fpath.is_file():
                    rel = fpath.relative_to(files_dir)
                    zf.write(str(fpath), str(rel))
        zip_buf.seek(0)
        return send_file(
            zip_buf,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"mysvn_version_{version_id}.zip",
        )
    except Exception as e:
        log.exception("download_version出错")
        return jsonify({"error": str(e)}), 500


@app.route("/api/download_selective/<int:version_id>", methods=["POST"])
def download_selective(version_id: int):
    """按文件列表选择性下载，仅打包请求的文件"""
    try:
        data: dict = request.get_json(force=True)
        wanted: list = data.get("files", [])
        if not wanted:
            return jsonify({"error": "文件列表为空"}), 400

        files_dir = version_files_dir(version_id)
        if not files_dir.exists():
            return jsonify({"error": f"版本 {version_id} 不存在"}), 404

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel_str in wanted:
                fpath = files_dir / rel_str
                if fpath.exists() and fpath.is_file():
                    zf.write(str(fpath), rel_str)
        zip_buf.seek(0)
        return send_file(
            zip_buf,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"mysvn_v{version_id}_selective.zip",
        )
    except Exception as e:
        log.exception("download_selective出错")
        return jsonify({"error": str(e)}), 500


@app.route("/api/delete_version/<int:version_id>", methods=["POST"])
def delete_version(version_id: int):
    """删除指定版本的文件和数据库记录"""
    try:
        db = get_db()

        row = db.execute("SELECT id FROM versions WHERE id = ?", (version_id,)).fetchone()
        if not row:
            db.close()
            return jsonify({"error": f"版本 {version_id} 不存在"}), 404

        db.execute("DELETE FROM file_records WHERE version_id = ?", (version_id,))
        db.execute("DELETE FROM versions WHERE id = ?", (version_id,))
        db.commit()
        db.close()

        files_dir = version_files_dir(version_id)
        if files_dir.exists():
            shutil.rmtree(str(files_dir), ignore_errors=True)

        manifest_path = MANIFEST_DIR / f"v{version_id}.json"
        if manifest_path.exists():
            manifest_path.unlink(missing_ok=True)

        log.info(f"版本 {version_id} 已删除")
        return jsonify({"success": True, "deleted_version": version_id})
    except Exception as e:
        log.exception("delete_version出错")
        return jsonify({"error": str(e)}), 500


@app.route("/api/manifest/<int:version_id>", methods=["GET"])
def get_manifest(version_id: int):
    try:
        manifest_path = MANIFEST_DIR / f"v{version_id}.json"
        if not manifest_path.exists():
            db = get_db()
            manifest = get_file_manifest(version_id, db)
            db.close()
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        else:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return jsonify({"manifest": manifest})
    except Exception as e:
        log.exception("get_manifest出错")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# UDP 广播线程
# ---------------------------------------------------------------------------
class UDPBroadcaster(threading.Thread):
    def __init__(self, port: int):
        super().__init__(daemon=True)
        self.port = port
        self._stop = threading.Event()

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception as e:
            log.error(f"创建UDP广播socket失败: {e}")
            return

        lan_ip = get_lan_ip()
        msg = json.dumps({
            "server": "MySVN",
            "ip": lan_ip,
            "port": self.port,
        }).encode("utf-8")

        log.info(f"UDP广播已启动: {lan_ip}:{self.port}")
        while not self._stop.is_set():
            try:
                sock.sendto(msg, ("255.255.255.255", UDP_BROADCAST_PORT))
            except Exception as e:
                log.debug(f"广播发送失败: {e}")
            self._stop.wait(BROADCAST_INTERVAL)
        try:
            sock.close()
        except Exception:
            pass

    def stop(self):
        self._stop.set()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def main():
    ensure_dirs()
    db = get_db()
    init_db(db)
    db.close()

    # 端口自动切换
    port = START_PORT
    for offset in range(MAX_PORT_RETRY):
        port = START_PORT + offset
        try:
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_sock.settimeout(1)
            test_sock.bind(("0.0.0.0", port))
            test_sock.close()
            break
        except OSError:
            if offset == MAX_PORT_RETRY - 1:
                log.error(f"端口 {START_PORT}-{START_PORT+MAX_PORT_RETRY-1} 均被占用，无法启动")
                input("按回车键退出...")
                sys.exit(1)
            log.warning(f"端口 {port} 被占用，尝试 {port+1}")

    broadcaster = UDPBroadcaster(port)
    broadcaster.start()

    lan_ip = get_lan_ip()
    print(f"\n{'='*50}")
    print(f"  My-SVN 服务端已启动")
    print(f"  地址: http://{lan_ip}:{port}")
    print(f"  存储目录: {DATA_DIR}")
    print(f"  按 Ctrl+C 停止服务")
    print(f"{'='*50}\n")

    try:
        app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        broadcaster.stop()
        log.info("服务端已停止")


if __name__ == "__main__":
    main()
