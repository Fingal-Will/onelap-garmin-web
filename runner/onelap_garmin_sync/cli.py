from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .auth import EncryptedOneLapSessionStore
from .config import Settings
from .fit_preprocess import FitPreprocessor
from .garmin import GarminUploader
from .ledger import Ledger
from .lock import ProcessLock
from .onelap import (
    OneLapAuthenticationError,
    OneLapClient,
    OneLapHTTPError,
)
from .service import SyncService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="OneLap 下载器 + Garmin 中国区/国际区双区上传器"
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("sync", "reconcile", "check-onelap", "init-db", "status"),
        default="sync",
    )
    parser.add_argument(
        "--full-scan",
        action="store_true",
        help="从第一页开始扫描全部 OneLap 历史活动",
    )
    parser.add_argument(
        "--retry-auth",
        action="store_true",
        help="立即重试 auth_required 状态，适合更新 OAuth Secret 后手动执行",
    )
    parser.add_argument(
        "--record-id",
        help="仅处理指定 OneLap record ID，适合单条活动验证",
    )
    parser.add_argument(
        "--force-reupload",
        action="store_true",
        help="重新排队指定活动的目标区域；必须同时提供 --record-id",
    )
    parser.add_argument(
        "--recent-limit",
        type=int,
        default=20,
        help="reconcile 使用：核对最近多少条 OneLap 活动（1-200）",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="输出更详细的调试日志"
    )
    return parser


def build_onelap_client(settings: Settings, ledger: Ledger) -> OneLapClient:
    return OneLapClient(
        token=settings.onelap_token,
        refresh_token=settings.onelap_refresh_token,
        account=settings.onelap_account,
        password=settings.onelap_password,
        token_store=EncryptedOneLapSessionStore(
            ledger,
            settings.onelap_session_key,
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.force_reupload and not args.record_id:
        parser.error("--force-reupload 必须与 --record-id 一起使用")
    record_id = args.record_id.strip() if args.record_id else None
    if args.record_id is not None and not record_id:
        parser.error("--record-id 不能为空")
    if not 1 <= args.recent_limit <= 200:
        parser.error("--recent-limit 必须在 1 到 200 之间")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    root_dir = Path(__file__).resolve().parent.parent
    try:
        settings = Settings.from_env(root_dir)
        settings.prepare_directories()
        ledger = Ledger(settings.database_path)
        try:
            if args.command == "init-db":
                print(f"SQLite 台账已初始化：{settings.database_path}")
                return 0
            if args.command == "status":
                rows = ledger.summary()
                if not rows:
                    print("台账中还没有同步任务")
                for row in rows:
                    print(
                        f"{row['target_region']:6} "
                        f"{row['status']:14} {row['count']}"
                    )
                return 0
            if args.command == "check-onelap":
                settings.validate_for_onelap()
                page = build_onelap_client(settings, ledger).fetch_page(1)
                count_message = (
                    f"共 {page.total} 条"
                    if page.total
                    else f"第一页读取到 {len(page.items)} 条"
                )
                print(
                    "OneLap 凭据验证成功："
                    f"HTTP 200，当前账号可读取活动列表（{count_message}）。"
                )
                return 0

            if args.command == "reconcile":
                settings.validate_for_reconcile()
            else:
                settings.validate_for_sync()
            with ProcessLock(settings.lock_path):
                fit_preprocessor = None
                if settings.preprocessing_enabled:
                    if settings.processed_dir is None:
                        raise ValueError("未配置 FIT 预处理输出目录")
                    fit_preprocessor = FitPreprocessor(settings.processed_dir)
                service = SyncService(
                    settings=settings,
                    ledger=ledger,
                    onelap=build_onelap_client(settings, ledger),
                    garmin=GarminUploader(settings.uploader_path),
                    fit_preprocessor=fit_preprocessor,
                )
                if args.command == "reconcile":
                    if args.full_scan or record_id or args.force_reupload:
                        parser.error(
                            "reconcile 不支持 --full-scan、--record-id "
                            "或 --force-reupload"
                        )
                    return service.reconcile_recent(
                        recent_limit=args.recent_limit,
                    )
                return service.run(
                    full_scan=args.full_scan,
                    retry_auth=args.retry_auth,
                    record_id=record_id,
                    force_reupload=args.force_reupload,
                )
        finally:
            ledger.close()
    except OneLapHTTPError as exc:
        logging.getLogger(__name__).error("OneLap 请求失败：%s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OneLapAuthenticationError as exc:
        logging.getLogger(__name__).error("OneLap 认证失败：%s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        logging.getLogger(__name__).exception("同步任务启动失败")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
