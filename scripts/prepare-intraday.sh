# 由 restart-worker.sh 加载，部署锁已持有，旧服务仍在运行。
prepare_intraday() (
    intraday_binary="$("$venv_python" -c 'from stock_research.config import settings; print(settings.intraday_binary.resolve() if settings.intraday_enabled else "")')" ||
        fail "无法读取盘中监控配置"
    [[ -n "$intraday_binary" ]] || return 0

    # 更新版本时同步核对 https://go.dev/dl/ 上对应架构的 SHA-256。
    go_version="go1.27.3"
    case "$(uname -m)" in
        x86_64)
            go_arch=amd64
            go_sha256=65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40
            ;;
        aarch64|arm64)
            go_arch=arm64
            go_sha256=a06e99309dbcbdca1a8ccd2282c3ee146e55930886e26b4f1193269587165f49
            ;;
        *) fail "盘中监控的 Go 自动安装支持 Linux x86_64 和 ARM64" ;;
    esac

    toolchain_root="$runtime/go-toolchains"
    toolchain="$toolchain_root/$go_version.linux-$go_arch"
    go_archive="$go_version.linux-$go_arch.tar.gz"
    go_expected="go version $go_version linux/$go_arch"
    unset GOROOT
    export GOTOOLCHAIN=local GOENV=off GOWORK=off GOFLAGS=
    export GOOS=linux GOARCH="$go_arch" CGO_ENABLED=0
    download_dir=
    build_file=
    trap '
        if [[ -n "$download_dir" ]]; then rm -rf -- "$download_dir"; fi
        if [[ -n "$build_file" ]]; then rm -f -- "$build_file"; fi
    ' EXIT

    if [[ ! -e "$toolchain" ]]; then
        for dependency in curl tar sha256sum; do
            command -v "$dependency" >/dev/null || fail "安装 Go 需要 $dependency"
        done
        mkdir -p -- "$toolchain_root"
        download_dir="$(mktemp -d -- "$toolchain_root/.download.XXXXXX")"
        printf '下载 Go 官方包 %s…\n' "$go_archive"
        curl -q --fail --location --silent --show-error --retry 2 --connect-timeout 10 --max-time 300 \
            --proto '=https' --proto-redir '=https' \
            --output "$download_dir/$go_archive" "https://go.dev/dl/$go_archive" ||
            fail "Go 下载失败，请检查服务器到 go.dev 和 dl.google.com 的连接"
        printf '%s  %s\n' "$go_sha256" "$go_archive" |
            (cd "$download_dir" && sha256sum --check --status) ||
            fail "Go 下载包的 SHA-256 校验失败"
        tar --no-same-owner -xzf "$download_dir/$go_archive" -C "$download_dir" ||
            fail "Go 下载包解压失败"
        [[ "$("$download_dir/go/bin/go" version)" == "$go_expected" ]] ||
            fail "Go 下载包的版本或架构与预期不符"
        mv -T -- "$download_dir/go" "$toolchain"
    fi

    go_bin="$toolchain/bin/go"
    [[ -x "$go_bin" && "$("$go_bin" version)" == "$go_expected" ]] ||
        fail "本地 Go 工具链无法运行，请检查 $toolchain"
    printf '使用 %s 构建盘中行情程序…\n' "$go_version"
    mkdir -p -- "$(dirname -- "$intraday_binary")"
    build_file="$(mktemp -- "${intraday_binary}.XXXXXX")"
    # 限制编译并行度，复用 Go 缓存。构建成功后才替换当前程序。
    (cd "$root/gateway/intraday" && GOMAXPROCS=2 "$go_bin" build -p 2 -mod=readonly -trimpath -o "$build_file" .) ||
        fail "Go 盘中行情程序构建失败"
    chmod 700 "$build_file"
    mv -fT -- "$build_file" "$intraday_binary"
    build_file=
)
