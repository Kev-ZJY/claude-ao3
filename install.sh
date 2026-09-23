#!/bin/sh
# Claude 凹3 binary installer. POSIX sh; no Python, administrator access or uv.
set -eu
umask 077

VERSION=0.6.0
ARCHIVE="claude-ao3-$VERSION-macos-arm64.tar.gz"
BASE="https://github.com/Kev-ZJY/claude-ao3/releases/download/v$VERSION"
COMMAND_MARKER='# Claude-ao3 managed command v1'
RELEASE_MARKER='# Claude-ao3 binary release v1'
BEGIN='# >>> Claude-ao3 PATH >>>'
END='# <<< Claude-ao3 PATH <<<'
stage=
command_tmp=
profile_tmp=
lock=

die() { code=$1; shift; printf '安装失败：%s\n' "$*" >&2; exit "$code"; }
cleanup() {
    [ -z "$command_tmp" ] || rm -f "$command_tmp"
    [ -z "$profile_tmp" ] || rm -f "$profile_tmp"
    [ -z "$stage" ] || rm -rf "$stage"
    [ -z "$lock" ] || rmdir "$lock" 2>/dev/null || :
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
usage() {
    cat <<'EOF'
Claude 凹3 0.6.0 macOS Apple Silicon 预览版（macOS 13+）
用法：sh install.sh [--install-root DIR] [--bin-dir DIR] [--no-path] [--launch]
  --install-root DIR  版本目录的父目录，默认 ~/.local/share/claude-ao3
  --bin-dir DIR       用户命令目录，默认 ~/.local/bin
  --no-path           不修改 shell 配置
  --launch            安装成功后连接 /dev/tty 启动；无交互终端则只完成安装
退出码：2 参数错误；3 不支持的平台/缺少工具；4 下载/校验/归档失败；5 安装冲突/写入失败。
EOF
}
[ -n "${HOME:-}" ] || die 2 'HOME 未设置。'
install_root="$HOME/.local/share/claude-ao3"
bin_dir="$HOME/.local/bin"
no_path=0
launch=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --install-root|--bin-dir)
            [ "$#" -ge 2 ] && [ -n "$2" ] || die 2 "$1 需要目录参数。"
            if [ "$1" = --install-root ]; then install_root=$2; else bin_dir=$2; fi
            shift 2 ;;
        --no-path) no_path=1; shift ;;
        --launch) launch=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; die 2 "未知参数：$1" ;;
    esac
done

[ "$(uname -s)" = Darwin ] || die 3 '本下载仅支持 macOS Apple Silicon；Linux 没有对应二进制。'
arch=$(uname -m)
case "$arch" in
    arm64) : ;;
    x86_64)
        [ "$(sysctl -in sysctl.proc_translated 2>/dev/null || :)" = 1 ] ||
            die 3 '本下载仅支持 Apple Silicon；尚未提供 Intel 二进制。' ;;
    *) die 3 "不支持的 CPU 架构：$arch" ;;
esac
macos=$(sw_vers -productVersion)
major=${macos%%.*}
case "$major" in ''|*[!0-9]*) die 3 '无法识别 macOS 版本。' ;; esac
[ "$major" -ge 13 ] || die 3 '本版本需要 macOS 13 或更新版本。'
for tool in curl tar shasum awk sed mktemp stat; do
    command -v "$tool" >/dev/null 2>&1 || die 3 "缺少系统工具：$tool"
done

# Newlines cannot be represented safely in the line-based ownership/profile files.
newline='
'
carriage=$(printf '\r')
for path in "$install_root" "$bin_dir"; do
    case "$path" in *"$newline"*|*"$carriage"*) die 2 '目录不能包含换行。' ;; esac
    [ ! -L "$path" ] || die 5 "拒绝通过目录符号链接安装：$path"
done
case "$bin_dir" in *:*) die 2 '命令目录不能包含 PATH 分隔符冒号。' ;; esac
case "$install_root" in /*) : ;; *) install_root="$PWD/$install_root" ;; esac
case "$bin_dir" in /*) : ;; *) bin_dir="$PWD/$bin_dir" ;; esac
owned_command() {
    [ -f "$1" ] && [ ! -L "$1" ] &&
        [ "$(sed -n '1p' "$1")" = '#!/bin/sh' ] &&
        [ "$(sed -n '2p' "$1")" = "$COMMAND_MARKER" ]
}
owned_release() {
    [ -d "$1" ] && [ ! -L "$1" ] &&
        [ -f "$1/.claude-ao3-release" ] && [ ! -L "$1/.claude-ao3-release" ] &&
        [ "$(sed -n '1p' "$1/.claude-ao3-release")" = "$RELEASE_MARKER" ] &&
        [ "$(sed -n '2p' "$1/.claude-ao3-release")" = "version=$VERSION" ]
}
check_targets() {
    if [ -e "$target" ] || [ -L "$target" ]; then
        owned_command "$target" || die 5 "拒绝覆盖非本产品管理的命令：$target"
    fi
    if [ -e "$version_dir" ] || [ -L "$version_dir" ]; then
        owned_release "$version_dir" || die 5 "拒绝覆盖未知版本目录：$version_dir"
    fi
}
target="$bin_dir/claude-ao3"
version_dir="$install_root/releases/$VERSION"
check_targets
mkdir -p "$install_root/releases" "$bin_dir" || die 5 '无法创建安装目录。'
install_root=$(cd "$install_root" && pwd -P)
bin_dir=$(cd "$bin_dir" && pwd -P)
[ ! -L "$install_root/releases" ] || die 5 '拒绝通过 releases 符号链接安装。'
target="$bin_dir/claude-ao3"
version_dir="$install_root/releases/$VERSION"
check_targets
mkdir "$install_root/.install-lock" 2>/dev/null || die 5 '另一安装正在进行，或存在未处理的 .install-lock；请先检查该目录。'
lock="$install_root/.install-lock"
stage=$(mktemp -d "$install_root/releases/.claude-ao3-install.XXXXXXXX") || die 5 '无法创建临时目录。'

download() {
    # -q must be first: user curlrc must not weaken TLS or change output paths.
    curl -q --proto '=https' --proto-redir '=https' --tlsv1.2 \
        --fail --location --silent --show-error --connect-timeout 15 --max-time 180 \
        --retry 2 --retry-delay 1 --retry-max-time 240 --max-filesize "$2" \
        --output "$stage/$1" "$BASE/$1" || die 4 "下载失败：$1（原命令保持不变）"
}
printf '正在下载 Claude 凹3 %s（macOS Apple Silicon）…\n' "$VERSION"
download SHA256SUMS.txt 65536
expected=$(awk -v name="$ARCHIVE" '
    NF == 0 { next }
    NF != 2 || length($1) != 64 || $1 !~ /^[0123456789abcdefABCDEF]+$/ { bad=1; next }
    { filename=$2; sub(/^\*/, "", filename); if (filename == name) { count++; digest=tolower($1) } }
    END { if (bad || count != 1) exit 1; print digest }
' "$stage/SHA256SUMS.txt") || die 4 'SHA256SUMS.txt 格式错误、缺少目标或包含重复目标。'
download "$ARCHIVE" 268435456
actual=$(shasum -a 256 "$stage/$ARCHIVE" | awk '{print $1}') || die 4 '无法计算 SHA-256。'
[ "$actual" = "$expected" ] || die 4 '下载包 SHA-256 不匹配（原命令保持不变）。'

# Release tar is deliberately dereferenced at build time: only files/directories.
# -P is used for LISTING only, so absolute malicious names cannot be hidden.
tar -P -tzf "$stage/$ARCHIVE" > "$stage/names" || die 4 '无法读取归档。'
awk '
    {
        name=$0; sub(/\/$/, "", name)
        if (name !~ /^claude-ao3(\/|$)/ || name ~ /[^A-Za-z0-9_.\/+@-]/ || seen[name]++) exit 1
        n=split(name, parts, "/")
        for (i=1;i<=n;i++) if (parts[i] == "" || parts[i] == "." || parts[i] == "..") exit 1
        if (name == "claude-ao3/.claude-ao3-release") exit 1
        count++
    }
    END { if (!count) exit 1 }
' "$stage/names" || die 4 '归档包含越界、重复或不受支持的路径。'
tar -P -tvzf "$stage/$ARCHIVE" > "$stage/types" || die 4 '无法检查归档类型。'
awk 'substr($0,1,1) != "-" && substr($0,1,1) != "d" { exit 1 }' "$stage/types" ||
    die 4 '归档包含链接或特殊文件；本发行只允许普通文件和目录。'
mkdir "$stage/payload" || die 5 '无法创建解压目录。'
COPYFILE_DISABLE=1 tar -xzf "$stage/$ARCHIVE" -C "$stage/payload" --no-same-owner --no-same-permissions ||
    die 4 '归档解压失败。'
payload="$stage/payload/claude-ao3"
[ -f "$payload/claude-ao3" ] && [ ! -L "$payload/claude-ao3" ] &&
    [ -x "$payload/claude-ao3" ] && [ -d "$payload/_internal" ] || die 4 '归档缺少可执行程序或运行库。'

# The whole version directory is committed first, then the tiny command atomically.
check_targets
if [ -d "$version_dir" ]; then
    [ "$(sed -n '3p' "$version_dir/.claude-ao3-release")" = "sha256=$expected" ] &&
        [ -f "$version_dir/claude-ao3" ] && [ ! -L "$version_dir/claude-ao3" ] &&
        [ -x "$version_dir/claude-ao3" ] && [ -d "$version_dir/_internal" ] &&
        [ ! -L "$version_dir/_internal" ] || die 5 '已有同版本目录不匹配或不完整；为保留当前安装，已停止。'
else
    printf '%s\nversion=%s\nsha256=%s\n' "$RELEASE_MARKER" "$VERSION" "$expected" > "$payload/.claude-ao3-release"
    mv "$payload" "$version_dir" || die 5 '无法提交新版本目录。'
fi
quote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\"'\\\"'/g")"; }
command_tmp=$(mktemp "$bin_dir/.claude-ao3-command.XXXXXXXX") || die 5 '无法创建命令临时文件。'
{
    printf '#!/bin/sh\n%s\nexec ' "$COMMAND_MARKER"
    quote "$version_dir/claude-ao3"
    printf ' "$@"\n'
} > "$command_tmp" || die 5 '无法写入命令。'
chmod 755 "$command_tmp" || die 5 '无法设置命令权限。'
check_targets
mv -f "$command_tmp" "$target" || die 5 '无法提交命令（原命令保持不变）。'
command_tmp=

update_profile() {
    profile=$1
    if [ -L "$profile" ] || { [ -e "$profile" ] && [ ! -f "$profile" ]; }; then
        printf '未修改非普通配置文件：%s；请手动配置 PATH。\n' "$profile" >&2
        return
    fi
    profile_parent=${profile%/*}
    if ! mkdir -p "$profile_parent"; then
        printf '无法创建 shell 配置目录；请手动配置 PATH。\n' >&2
        return
    fi
    profile_tmp=$(mktemp "$profile_parent/.claude-ao3-path.XXXXXXXX") || return 0
    source_profile=/dev/null
    mode=600
    if [ -f "$profile" ]; then
        source_profile=$profile
        mode=$(stat -f '%Lp' "$profile") || mode=600
    fi
    if ! awk -v begin="$BEGIN" -v end="$END" '
        $0 == begin { if (inside) bad=1; inside=1; next }
        $0 == end { if (!inside) bad=1; inside=0; next }
        !inside { print }
        END { if (inside || bad) exit 1 }
    ' "$source_profile" > "$profile_tmp"; then
        printf 'PATH 标记块不完整，未修改：%s\n' "$profile" >&2
        rm -f "$profile_tmp"; profile_tmp=
        return
    fi
    {
        printf '%s\nexport PATH=' "$BEGIN"
        quote "$bin_dir"
        printf ':"$PATH"\n%s\n' "$END"
    } >> "$profile_tmp" || {
        printf '无法写入 shell 配置；请手动配置 PATH。\n' >&2
        rm -f "$profile_tmp"; profile_tmp=
        return
    }
    if chmod "$mode" "$profile_tmp" && mv -f "$profile_tmp" "$profile"; then
        profile_tmp=
    else
        printf '无法更新 shell 配置；已安装的命令仍可用：%s\n' "$target" >&2
        rm -f "$profile_tmp"; profile_tmp=
    fi
}
if [ "$no_path" = 0 ]; then
    case ":${PATH:-}:" in
        *":$bin_dir:"*) : ;;
        *)
            shell_name=${SHELL:-}
            case "${shell_name##*/}" in
                zsh) update_profile "${ZDOTDIR:-$HOME}/.zshrc" ;;
                bash)
                    update_profile "$HOME/.bashrc"
                    login_profile="$HOME/.bash_profile"
                    for name in .bash_profile .bash_login .profile; do
                        if [ -e "$HOME/$name" ] || [ -L "$HOME/$name" ]; then login_profile="$HOME/$name"; break; fi
                    done
                    update_profile "$login_profile" ;;
                *) printf '当前 shell 未自动配置 PATH。\n' ;;
            esac ;;
    esac
fi
printf 'Claude 凹3 %s 已安装：%s\n新开终端运行：claude-ao3\n当前终端可直接运行：' "$VERSION" "$target"
quote "$target"
printf '\n预览版仅作临时签名，首次打开可能需要 macOS 安全确认；安装器不会更改系统安全设置。\n'
if [ "$launch" = 1 ]; then
    if ( : </dev/tty >/dev/tty 2>/dev/tty ) 2>/dev/null; then
        cleanup
        stage=; lock=
        trap - EXIT INT TERM HUP
        exec "$target" </dev/tty >/dev/tty 2>/dev/tty
    else
        printf '已安装；当前没有交互终端，未启动。请在终端运行以上命令。\n'
    fi
fi
