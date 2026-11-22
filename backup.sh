#!/usr/bin/env bash
# بکاپ از دیتابیس (موجودی کاربران و سفارش‌ها). ۱۴ بکاپ آخر نگه داشته می‌شود.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$DIR/backups"
STAMP=$(date +%Y%m%d_%H%M%S)
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$DIR/bot.db" ".backup '$DIR/backups/bot_$STAMP.db'"
else
  cp "$DIR/bot.db" "$DIR/backups/bot_$STAMP.db"
fi
ls -1t "$DIR"/backups/bot_*.db | tail -n +15 | xargs -r rm --
echo "backup saved: backups/bot_$STAMP.db"
