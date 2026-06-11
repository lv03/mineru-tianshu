#!/usr/bin/env bash
#
# 天枢冒烟测试：提交任务 → 轮询完成 → 测签名 URL(403/篡改/有效) → 删除 → 校验目录清理
#
# 用法:
#   bash scripts/smoke_test.sh [待解析文件路径]
#
# 环境变量(可选):
#   API_BASE   API 根地址，默认 http://localhost:8000/api/v1
#   ADMIN_USER 登录用户名，默认 admin
#   ADMIN_PASS 登录密码，默认 admin123
#   BACKEND    解析后端，默认 auto
#   POLL_TIMEOUT 轮询超时秒数，默认 300
#
# 退出码: 0=全部通过, 非0=有失败
set -u

API_BASE="${API_BASE:-http://localhost:8000/api/v1}"
HOST="${API_BASE%/api/v1}"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-admin123}"
BACKEND="${BACKEND:-auto}"
POLL_TIMEOUT="${POLL_TIMEOUT:-300}"
SMOKE_FILE="${1:-}"

PASS=0
FAIL=0
ok()   { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad()  { echo "  ❌ $1"; FAIL=$((FAIL+1)); }
info() { echo "→ $1"; }
die()  { echo "💥 $1"; exit 2; }

# JSON 取值: jget '<json>' '<python表达式, d 为已解析对象>'
jget() { python3 -c 'import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$2" <<<"$1" 2>/dev/null; }

command -v curl >/dev/null || die "需要 curl"
command -v python3 >/dev/null || die "需要 python3"

# 统一的 curl：绕过本机代理(避免 http_proxy/all_proxy 把 localhost 也走代理导致空响应)
CURL=(curl -s --noproxy '127.0.0.1,localhost,::1')
req()        { "${CURL[@]}" "$@"; }
http_code()  { "${CURL[@]}" -o /dev/null -w '%{http_code}' "$@"; }

echo "============================================================"
echo "天枢冒烟测试  @ $API_BASE"
echo "============================================================"

# ---- 连通性预检 ----
info "连通性预检 $HOST ..."
PING_CODE="$(http_code "$API_BASE/health" || echo 000)"
if [ "$PING_CODE" = "000" ]; then
  echo "  ❌ 无法连接 $HOST (HTTP 000)"
  echo "     排查: 1) 后端是否在运行(python start_all.py)?  2) 端口/地址是否为 $HOST ?"
  echo "          3) 是否设置了 http_proxy/all_proxy 把 localhost 也代理了(本脚本已加 --noproxy，"
  echo "             若仍失败可尝试: unset http_proxy https_proxy all_proxy)"
  die "API 不可达，终止"
fi
ok "API 可达 (/health HTTP $PING_CODE)"

# ---- 0. 准备待解析文件 ----
TMP_CLEAN=""
if [ -z "$SMOKE_FILE" ]; then
  SMOKE_FILE="$(mktemp -t smoke_XXXX).txt"
  TMP_CLEAN="$SMOKE_FILE"
  printf '# 冒烟测试文档\n\n这是一个用于天枢冒烟测试的最小文本文件。\n\n- 项目一\n- 项目二\n' > "$SMOKE_FILE"
  info "未指定文件，已生成临时测试文件: $SMOKE_FILE"
else
  [ -f "$SMOKE_FILE" ] || die "文件不存在: $SMOKE_FILE"
  info "使用文件: $SMOKE_FILE"
fi

# ---- 1. 登录 ----
info "登录获取 token..."
LOGIN_RESP="$(req -X POST "$API_BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}")"
TOKEN="$(jget "$LOGIN_RESP" 'd.get("access_token","")')"
if [ -z "$TOKEN" ]; then
  LOGIN_CODE="$(http_code -X POST "$API_BASE/auth/login" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}")"
  echo "  ❌ 登录失败 (HTTP $LOGIN_CODE)"
  echo "     响应体: ${LOGIN_RESP:-<空>}"
  echo "     若 401: 用户名/密码不对(默认 admin/admin123，或已改密)。"
  die "登录失败，终止"
fi
ok "登录成功，已获取 token"
AUTH=(-H "Authorization: Bearer $TOKEN")

# ---- 2. 提交任务 ----
info "提交任务 (backend=$BACKEND)..."
SUBMIT_RESP="$(req -X POST "$API_BASE/tasks/submit" "${AUTH[@]}" \
  -F "file=@$SMOKE_FILE" -F "backend=$BACKEND")"
TID="$(jget "$SUBMIT_RESP" 'd.get("task_id","")')"
[ -n "$TID" ] || die "提交失败: $SUBMIT_RESP"
ok "任务已提交: $TID"

# ---- 3. 取 source_url 测签名鉴权(提交后即可，无需等完成) ----
STATUS_RESP="$(req "$API_BASE/tasks/$TID?format=both" "${AUTH[@]}")"
SRC_PATH="$(jget "$STATUS_RESP" 'd.get("source_url","") or ""')"
if [ -n "$SRC_PATH" ]; then
  SIGNED_URL="$HOST$SRC_PATH"
  NOSIG_URL="$HOST${SRC_PATH%%\?*}"                          # 去掉 ?exp&sig
  TAMPER_URL="$(python3 -c '
import sys,urllib.parse as u
base=sys.argv[1]; full=sys.argv[2]
p=u.urlparse(full); q=u.parse_qs(p.query); q["sig"]=["deadbeef"]
print(base+p.path+"?"+u.urlencode({k:v[0] for k,v in q.items()}))' "$HOST" "$SIGNED_URL")"

  info "测试本地文件签名 URL 鉴权..."
  C1="$(http_code "$NOSIG_URL")";  [ "$C1" = "403" ] && ok "无签名访问 → 403" || bad "无签名应 403，实际 $C1"
  C2="$(http_code "$TAMPER_URL")"; [ "$C2" = "403" ] && ok "篡改签名 → 403" || bad "篡改签名应 403，实际 $C2"
  C3="$(http_code "$SIGNED_URL")"; [ "$C3" = "200" ] && ok "有效签名 → 200" || bad "有效签名应 200，实际 $C3"
else
  bad "未取到 source_url，跳过签名测试"
fi

# ---- 4. 轮询直到完成/失败 ----
info "轮询任务状态(最多 ${POLL_TIMEOUT}s)..."
ELAPSED=0; STATUS=""; RESULT_PATH=""
while [ "$ELAPSED" -lt "$POLL_TIMEOUT" ]; do
  STATUS_RESP="$(req "$API_BASE/tasks/$TID?format=both" "${AUTH[@]}")"
  STATUS="$(jget "$STATUS_RESP" 'd.get("status","")')"
  case "$STATUS" in
    completed) RESULT_PATH="$(jget "$STATUS_RESP" 'd.get("result_path","") or ""')"; break ;;
    failed)    break ;;
  esac
  sleep 3; ELAPSED=$((ELAPSED+3))
  printf '.'
done
echo ""
if [ "$STATUS" = "completed" ]; then
  ok "任务完成，result_path=$RESULT_PATH"
  # 完成后再测一次输出图片/PDF 的签名 URL(若有)
  PDF_URL="$(jget "$STATUS_RESP" 'd.get("data",{}).get("pdf_url","") if d.get("data") else ""')"
  if [ -n "$PDF_URL" ]; then
    CP="$(http_code "$HOST$PDF_URL")"; [ "$CP" = "200" ] && ok "预览 PDF 签名 URL → 200" || bad "预览 PDF 应 200，实际 $CP"
  fi
elif [ "$STATUS" = "failed" ]; then
  bad "任务失败(可能是解析环境问题，不影响签名/删除测试): $(jget "$STATUS_RESP" 'd.get("error_message","")')"
else
  bad "轮询超时，最终状态: $STATUS（仍继续测删除）"
fi

# ---- 5. 删除任务并校验目录清理 ----
info "删除任务并校验磁盘清理..."
DEL_RESP="$(req -X DELETE "$API_BASE/tasks/$TID" "${AUTH[@]}")"
DEL_OK="$(jget "$DEL_RESP" 'd.get("success",False)')"
[ "$DEL_OK" = "True" ] && ok "删除接口返回 success" || bad "删除失败: $DEL_RESP"

# 5a. result_path 目录应已删除(若之前完成)
if [ -n "$RESULT_PATH" ]; then
  if [ -d "$RESULT_PATH" ]; then bad "结果目录仍存在(应被删除): $RESULT_PATH"
  else ok "结果目录已物理删除: $RESULT_PATH"; fi
fi

# 5b. DB 记录应消失 → GET 返回 404
CODE_AFTER="$(http_code "$API_BASE/tasks/$TID" "${AUTH[@]}")"
[ "$CODE_AFTER" = "404" ] && ok "任务记录已删除(GET → 404)" || bad "删除后 GET 应 404，实际 $CODE_AFTER"

# ---- 清理临时文件 ----
[ -n "$TMP_CLEAN" ] && rm -f "$TMP_CLEAN"

echo "============================================================"
echo "结果:  通过 $PASS  失败 $FAIL"
echo "============================================================"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
