#!/usr/bin/env python3
"""
自动签到脚本
"""

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime
from dotenv import load_dotenv
from utils.config import AppConfig
from utils.notify import notify
from utils.balance_hash import load_balance_hash, save_balance_hash
from checkin import CheckIn

load_dotenv(override=True)

BALANCE_HASH_FILE = "balance_hash.txt"


def persist_xiaobai_token_updates(updates: list[dict]) -> None:
    """将刷新后的小白 token 写入 GitHub Actions 后续步骤可读取的临时文件。"""
    if not updates:
        return

    accounts_source = os.getenv("ACCOUNTS_SOURCE", "secret").lower()
    if accounts_source == "input":
        print("⚠️ Xiaobai token refreshed, but ACCOUNTS came from workflow input; GitHub secret will not be updated")
        return

    output_file = os.getenv("XIAOBAI_UPDATED_ACCOUNTS_FILE")
    if not output_file:
        print("⚠️ Xiaobai token refreshed, but XIAOBAI_UPDATED_ACCOUNTS_FILE is not configured")
        return

    accounts_str = os.getenv("ACCOUNTS")
    if not accounts_str:
        print("⚠️ Xiaobai token refreshed, but ACCOUNTS environment variable is missing")
        return

    try:
        accounts_data = json.loads(accounts_str)
    except json.JSONDecodeError as e:
        print(f"⚠️ Xiaobai token refreshed, but ACCOUNTS JSON cannot be parsed: {e}")
        return

    if not isinstance(accounts_data, list):
        print("⚠️ Xiaobai token refreshed, but ACCOUNTS is not a JSON array")
        return

    updated_count = 0
    for update in updates:
        account_index = update.get("account_index")
        if not isinstance(account_index, int) or account_index < 0 or account_index >= len(accounts_data):
            print(f"⚠️ Skip Xiaobai token update with invalid account index: {account_index}")
            continue

        account = accounts_data[account_index]
        if not isinstance(account, dict):
            print(f"⚠️ Skip Xiaobai token update for non-object account index: {account_index}")
            continue

        account["xiaobai_token"] = {
            "access_token": update["access_token"],
            "refresh_token": update["refresh_token"],
        }
        updated_count += 1

    if updated_count == 0:
        print("⚠️ Xiaobai token refreshed, but no ACCOUNTS entry was updated")
        return

    output_dir = os.path.dirname(output_file)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(accounts_data, f, ensure_ascii=False, separators=(",", ":"))

    runner_temp = os.getenv("RUNNER_TEMP") or output_dir or "."
    flag_file = os.path.join(runner_temp, "xiaobai_accounts_updated.flag")
    with open(flag_file, "w", encoding="utf-8") as f:
        f.write("true")

    print(f"✅ Prepared refreshed Xiaobai token update for {updated_count} account(s)")


def generate_balance_hash(balances: dict) -> str:
    """生成余额数据的hash"""
    # 将包含 quota 和 used 的结构转换为 {account_name: [quota]} 格式用于 hash 计算
    simple_balances = {}
    if balances:
        for account_key, account_balances in balances.items():
            quota_list = []
            for _, balance_info in account_balances.items():
                quota_list.append(balance_info["quota"])
            simple_balances[account_key] = quota_list

    balance_json = json.dumps(simple_balances, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(balance_json.encode("utf-8")).hexdigest()[:16]


async def main():
    """运行签到流程

    Returns:
            退出码: 0 表示至少有一个账号成功, 1 表示全部失败
    """

    print("🚀 newapi.ai multi-account auto check-in script started (using Camoufox)")
    print(f'🕒 Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

    app_config = AppConfig.load_from_env()
    print(f"⚙️ Loaded {len(app_config.providers)} provider(s)")

    # 检查账号配置
    if not app_config.accounts:
        print("❌ Unable to load account configuration, program exits")
        return 1
    
    print(f"⚙️ Found {len(app_config.accounts)} account(s)")

    # 加载余额hash
    last_balance_hash = load_balance_hash(BALANCE_HASH_FILE)

    # 为每个账号执行签到
    success_count = 0
    total_count = 0
    notification_content = []
    current_balances = {}
    need_notify = False  # 是否需要发送通知
    xiaobai_token_updates = []

    for i, account_config in enumerate(app_config.accounts):
        account_key = f"account_{i + 1}"
        account_name = account_config.get_display_name(i)
        if len(notification_content) > 0:
            notification_content.append("\n-------------------------------")

        try:
            provider_config = app_config.get_provider(account_config.provider)
            if not provider_config:
                print(f"❌ {account_name}: Provider '{account_config.provider}' configuration not found")
                need_notify = True
                notification_content.append(
                    f"[FAIL] {account_name}: Provider '{account_config.provider}' configuration not found"
                )
                continue

            print(f"🌀 Processing {account_name} using provider '{account_config.provider}'")
            checkin = CheckIn(account_name, account_config, provider_config, global_proxy=app_config.global_proxy)
            results = await checkin.execute()

            total_count += len(results)

            # 处理多个认证方式的结果
            account_success = False
            successful_methods = []
            failed_methods = []

            this_account_balances = {}
            # 构建详细的结果报告
            account_result = f"📣 {account_name} Summary:\n"
            for auth_method, success, user_info in results:
                status = "✅ SUCCESS" if success else "❌ FAILED"
                account_result += f"  {status} with {auth_method} authentication\n"

                if user_info and user_info.get("xiaobai_token_refreshed"):
                    refreshed_token = user_info.get("xiaobai_token", {})
                    access_token = refreshed_token.get("access_token")
                    refresh_token = refreshed_token.get("refresh_token")
                    if access_token and refresh_token:
                        xiaobai_token_updates.append(
                            {
                                "account_index": i,
                                "access_token": access_token,
                                "refresh_token": refresh_token,
                            }
                        )

                if success and user_info and user_info.get("success"):
                    account_success = True
                    success_count += 1
                    successful_methods.append(auth_method)
                    account_result += f"    💰 {user_info['display']}\n"
                    # 记录余额信息
                    current_quota = user_info["quota"]
                    current_used = user_info["used_quota"]
                    current_bonus = user_info["bonus_quota"]
                    this_account_balances[f"{auth_method}"] = {
                        "quota": current_quota,
                        "used": current_used,
                        "bonus": current_bonus,
                    }
                else:
                    failed_methods.append(auth_method)
                    error_msg = user_info.get("error", "Unknown error") if user_info else "Unknown error"
                    account_result += f"    🔺 {str(error_msg)}\n"

            if account_success:
                current_balances[account_key] = this_account_balances

            # 如果所有认证方式都失败，需要通知
            if not account_success and results:
                need_notify = True
                print(f"🔔 {account_name} all authentication methods failed, will send notification")

            # 如果有失败的认证方式，也通知
            if failed_methods and successful_methods:
                need_notify = True
                print(f"🔔 {account_name} has some failed authentication methods, will send notification")

            # 添加统计信息
            success_count_methods = len(successful_methods)
            failed_count_methods = len(failed_methods)

            account_result += f"\n📊 Statistics: {success_count_methods}/{len(results)} methods successful"
            if failed_count_methods > 0:
                account_result += f" ({failed_count_methods} failed)"

            notification_content.append(account_result)

        except Exception as e:
            print(f"❌ {account_name} processing exception: {e}")
            need_notify = True  # 异常也需要通知
            notification_content.append(f"❌ {account_name} Exception: {str(e)[:100]}...")

    # 检查余额变化
    current_balance_hash = generate_balance_hash(current_balances) if current_balances else None
    print(f"\n\nℹ️ Current balance hash: {current_balance_hash}, Last balance hash: {last_balance_hash}")
    if current_balance_hash:
        if last_balance_hash is None:
            # 首次运行
            need_notify = True
            print("🔔 First run detected, will send notification with current balances")
        elif current_balance_hash != last_balance_hash:
            # 余额有变化
            need_notify = True
            print("🔔 Balance changes detected, will send notification")
        else:
            print("ℹ️ No balance changes detected")

    # 保存当前余额hash
    if current_balance_hash:
        save_balance_hash(BALANCE_HASH_FILE, current_balance_hash)

    # 保存刷新后的小白 token，供 GitHub Actions 后续步骤回写 Environment Secret
    persist_xiaobai_token_updates(xiaobai_token_updates)

    if need_notify and notification_content:
        # 构建通知内容
        summary = [
            "-------------------------------",
            "📢 Check-in result statistics:",
            f"🔵 Success: {success_count}/{total_count}",
            f"🔴 Failed: {total_count - success_count}/{total_count}",
        ]

        if success_count == total_count:
            summary.append("✅ All accounts check-in successful!")
        elif success_count > 0:
            summary.append("⚠️ Some accounts check-in successful")
        else:
            summary.append("❌ All accounts check-in failed")

        time_info = f'🕓 Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'

        notify_content = "\n\n".join([time_info, "\n".join(notification_content), "\n".join(summary)])

        print(notify_content)
        notify.push_message("Check-in Alert", notify_content, msg_type="text")
        print("🔔 Notification sent due to failures or balance changes")
    else:
        print("ℹ️ All accounts successful and no balance changes detected, notification skipped")

    # 设置退出码
    sys.exit(0 if success_count > 0 else 1)


def run_main():
    """运行主函数的包装函数"""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⚠️ Program interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error occurred during program execution: {e}")
        sys.exit(1)


if __name__ == "__main__":
    run_main()
