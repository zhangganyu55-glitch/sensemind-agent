"""Local operator CLI for pairing codes and credential revocation.

Run from the project directory:

    python -m guardian.pairing                 # issue a 10-minute, single-use code
    python -m guardian.pairing --name 家属 --ttl 5
    python -m guardian.pairing --list-credentials
    python -m guardian.pairing --revoke-credential <credential-id>

This is a local database operation; nothing is exposed over the network.
"""
import argparse
import os

from .apk_storage import ApkStorage


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="守护天使本地配对管理")
    parser.add_argument("--db", default=os.getenv("GUARDIAN_DB", "guardian.db"),
                        help="SQLite path (default: GUARDIAN_DB or guardian.db)")
    parser.add_argument("--name", default="守护天使用户", help="显示在 App 中的名称")
    parser.add_argument("--ttl", type=int, default=10, help="有效期（分钟，1-60）")
    parser.add_argument("--code", default=None, help="指定配对码（12-100 字符，一般不填）")
    parser.add_argument("--role", default="legacy", choices=("wearer", "caregiver", "legacy"),
                        help="新凭证角色：wearer 上传设备数据，caregiver 只读并确认事件；"
                             "legacy 仅用于旧客户端兼容")
    parser.add_argument("--list-credentials", action="store_true",
                        help="列出凭证元数据（不显示 token）")
    parser.add_argument("--revoke-credential", metavar="CREDENTIAL_ID",
                        help="撤销指定凭证，立即失效")
    args = parser.parse_args(argv)

    storage = ApkStorage(args.db)

    if args.list_credentials:
        rows = storage.list_credentials()
        if not rows:
            print("没有凭证。")
            return 0
        for row in rows:
            state = "revoked" if row["revoked"] else "active"
            print(f"{row['id']}\t{row['guardian_id']}\t{row['name']}\t"
                  f"role={row.get('role', 'legacy')}\tcreated={row['created_at']}\t"
                  f"expires={row['expires_at']}\t{state}")
        return 0

    if args.revoke_credential:
        if storage.revoke_credential(args.revoke_credential):
            print("已撤销该凭证；现有 Bearer token 立即失效。")
            return 0
        print("未找到该凭证。")
        return 1

    issued = storage.issue_pairing_code(args.name, ttl_minutes=args.ttl,
                                        code=args.code, role=args.role)
    print(f"配对码：{issued['code']}")
    print(f"角色：{issued['role']}（仅可使用一次）")
    print(f"有效期至：{issued['expires_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
