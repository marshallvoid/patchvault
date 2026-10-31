import argparse
import sys
import threading
import time

from .config import CHECK_INTERVAL, CHECK_RETRY, WORKERS, base_dir
from .system import remove_old_program
from .vault import Vault


def periodic_check(vault):
    while True:
        vault.check()
        vault.check_app_update()
        vault.refresh_titles()
        time.sleep(CHECK_RETRY if vault.check_error else CHECK_INTERVAL)


def run_check(vault):
    vault.check()
    vault.check_app_update()
    if vault.app_update:
        print(f"Có phiên bản PatchVault mới: {vault.app_update['tag']} ({vault.app_update['url']})")
    if not vault.catalog:
        print("Chưa có catalog. Hãy mở ứng dụng để thêm catalog.")
        return 0
    if vault.check_error:
        print(f"Lỗi kiểm tra catalog: {vault.check_error}", file=sys.stderr)
        return 1
    updates = [entry for entry in vault.items if vault.status_of(entry) == "update"]
    missing = sum(vault.status_of(entry) == "missing" for entry in vault.items)
    for entry in updates:
        old = vault.state.get(entry["id"], {}).get("version") or "?"
        print(f"Có cập nhật: {entry['name']} ({old} -> {entry['version'] or '?'})")
    print(
        f"{vault.catalog['name']}: {len(updates)} bản cập nhật, {missing} game chưa tải, tổng {len(vault.items)} game."
    )
    return 0


def main():
    parser = argparse.ArgumentParser(description="Tải, theo dõi và đóng gói lại mod game.")
    parser.add_argument(
        "--dir",
        default=str(base_dir()),
        help="thư mục gốc của dữ liệu, file tải về nằm trong downloaded/",
    )
    parser.add_argument("--check", action="store_true", help="chỉ kiểm tra cập nhật rồi thoát")
    args = parser.parse_args()

    vault = Vault(args.dir)
    if args.check:
        return run_check(vault)
    remove_old_program()
    try:
        from .ui.app import App  # Here, so that --check works without Tkinter.
    except ImportError as error:
        if error.name not in ("tkinter", "_tkinter"):
            raise
        print(
            "Máy này chưa có Tkinter. Hãy cài gói tk (Arch) hoặc python3-tk (Debian/Ubuntu).",
            file=sys.stderr,
        )
        return 1

    threading.Thread(target=periodic_check, args=(vault,), daemon=True).start()
    for _ in range(WORKERS):
        threading.Thread(target=vault.worker, daemon=True).start()
    app = App(vault)
    if not vault.catalogs:
        app.root.after(300, app.open_manager)
    app.run()
    return 0
