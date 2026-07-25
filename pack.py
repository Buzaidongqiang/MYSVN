#!/usr/bin/env python3
"""My-SVN pack script - installs deps first, then packs via PyInstaller API"""
import os, sys, shutil, subprocess

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)

print("=" * 50)
print("  My-SVN Pack Tool")
print("=" * 50)

# Step 1: Install dependencies
print("\n[1/4] Installing dependencies...")
subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "-q"],
               check=True)
print("[OK] Dependencies installed")

# Step 2: Install PyInstaller
print("\n[2/4] Installing PyInstaller...")
subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller", "-q"],
               check=True)
print("[OK] PyInstaller installed")

# Step 3: Clean old files
print("\n[3/4] Cleaning old build files...")
for f in ["MySVN-Server.exe", "MySVN-Client.exe"]:
    p = os.path.join(BASE, f)
    if os.path.exists(p): os.remove(p)
for d in ["dist", "build"]:
    p = os.path.join(BASE, d)
    if os.path.exists(p): shutil.rmtree(p, ignore_errors=True)
for f in os.listdir(BASE):
    if f.endswith(".spec"): os.remove(os.path.join(BASE, f))
print("[OK] Clean done")

# Step 4: Pack server
print("\n[4/4] Packing MySVN-Server.exe (3-5 min)...")
sys.stdout.flush()
import PyInstaller.__main__
PyInstaller.__main__.run([
    '--onefile', '--console',
    '--distpath', BASE,
    'server.py',
    '-n', 'MySVN-Server.exe'
])
print("[OK] Server packed")

# Step 5: Pack client
print("\n[5/5] Packing MySVN-Client.exe (3-5 min)...")
sys.stdout.flush()
PyInstaller.__main__.run([
    '--onefile', '--windowed',
    '--distpath', BASE,
    'client.py',
    '-n', 'MySVN-Client.exe'
])
print("[OK] Client packed")

# Verify
print("\n" + "=" * 50)
print("  Pack completed! Generated files:")
print("=" * 50)
for f in ["MySVN-Server.exe", "MySVN-Client.exe"]:
    fp = os.path.join(BASE, f)
    if os.path.exists(fp):
        size = os.path.getsize(fp)
        print(f"  [OK] {f} ({size/1024/1024:.1f} MB)")
    else:
        print(f"  [WARN] {f} NOT FOUND")
print()
input("Press Enter to exit...")
