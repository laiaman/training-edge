#!/usr/bin/env bash
# 将三份计划真源/投影白名单同步到 OneDrive 备份副本。
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "$0")" && pwd)"
WORKSPACE_DIR="$(cd -P "$SCRIPT_DIR/../.." && pwd)"
TARGET_DIR="${TRAININGEDGE_ONEDRIVE_BACKUP_DIR:-$HOME/Library/CloudStorage/OneDrive-个人/AICoachPortable}"
DRY_RUN=0

usage() { echo "用法: bash scripts/backup_plan_to_onedrive.sh [--dry-run] [--target DIR]"; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --target) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; TARGET_DIR="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
    esac
done

case "$TARGET_DIR" in
    "$WORKSPACE_DIR"|"$WORKSPACE_DIR"/*) echo "目标不能位于本地运行项目内：$TARGET_DIR" >&2; exit 1 ;;
esac

FILES="
vault/plans/training_plan.yaml
vault/goals/current_goal.md
vault/plans/2026_Marathon_Plan.md
"

for rel in $FILES; do
    src="$WORKSPACE_DIR/$rel"
    [ -f "$src" ] || { echo "白名单源文件缺失：$src" >&2; exit 1; }
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "DRY-RUN  $src -> $TARGET_DIR/$rel"
    fi
done

if [ "$DRY_RUN" -eq 1 ]; then
    echo "✓ Dry-run 完成：仅白名单 3/3，未写入"
    exit 0
fi

stamp="$(date '+%Y%m%d-%H%M%S')"
stage="$TARGET_DIR/.trainingedge-plan-staging.$stamp.$$"
history="$TARGET_DIR/.trainingedge-plan-history/$stamp"
applied=""
created=""

rollback() {
    status=$?
    if [ "$status" -ne 0 ] && [ -n "$applied" ]; then
        echo "同步失败，正在恢复已覆盖的目标文件……" >&2
        for rel in $applied; do
            if [ -f "$history/$rel" ]; then
                mkdir -p "$(dirname "$TARGET_DIR/$rel")"
                cp -p "$history/$rel" "$TARGET_DIR/$rel"
            fi
        done
        for rel in $created; do rm -f -- "$TARGET_DIR/$rel"; done
    fi
    rm -rf -- "$stage"
    exit "$status"
}
trap rollback EXIT INT TERM

mkdir -p "$stage" "$history"
for rel in $FILES; do
    mkdir -p "$stage/$(dirname "$rel")"
    cp -p "$WORKSPACE_DIR/$rel" "$stage/$rel"
    src_sha="$(shasum -a 256 "$WORKSPACE_DIR/$rel" | awk '{print $1}')"
    stage_sha="$(shasum -a 256 "$stage/$rel" | awk '{print $1}')"
    [ "$src_sha" = "$stage_sha" ] || { echo "暂存 checksum 不匹配：$rel" >&2; exit 1; }
done

for rel in $FILES; do
    dest="$TARGET_DIR/$rel"
    if [ -f "$dest" ]; then
        mkdir -p "$history/$(dirname "$rel")"
        cp -p "$dest" "$history/$rel"
    else
        created="$created $rel"
    fi
    mkdir -p "$(dirname "$dest")"
    cp -p "$stage/$rel" "$dest.new.$$"
    mv -f "$dest.new.$$" "$dest"
    applied="$applied $rel"
    src_sha="$(shasum -a 256 "$WORKSPACE_DIR/$rel" | awk '{print $1}')"
    dest_sha="$(shasum -a 256 "$dest" | awk '{print $1}')"
    [ "$src_sha" = "$dest_sha" ] || { echo "目标 checksum 回读失败：$rel" >&2; exit 1; }
    echo "✓ $rel  $dest_sha"
done

rm -rf -- "$stage"
trap - EXIT INT TERM
echo "✓ 白名单备份完成（3/3）"
echo "  目标：$TARGET_DIR"
echo "  覆盖前版本：$history"
