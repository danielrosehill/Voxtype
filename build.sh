#!/bin/bash
# Build Voxtype packages
# Usage: ./build.sh [--deb|--dev|--help]

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

show_help() {
    echo "Voxtype Build System"
    echo ""
    echo "Usage: ./build.sh [COMMAND]"
    echo ""
    echo "Commands:"
    echo "  --deb [VERSION]    Build Debian package"
    echo "  --dev              Fast dev build + install"
    echo "  --help             Show this help"
}

get_version() {
    if [ -n "$1" ]; then
        echo "$1"
    else
        grep -oP 'version = "\K[^"]+' "$SCRIPT_DIR/pyproject.toml"
    fi
}

build_deb() {
    local VERSION=$(get_version "$1")
    local PKG_NAME="ai-typer-v2"
    local DIST_DIR="$SCRIPT_DIR/dist"
    local BUILD_DIR="$DIST_DIR/build/${PKG_NAME}_${VERSION}"

    echo "Building ${PKG_NAME} v${VERSION} .deb..."

    mkdir -p "$BUILD_DIR/DEBIAN"
    mkdir -p "$BUILD_DIR/opt/${PKG_NAME}"
    mkdir -p "$BUILD_DIR/usr/share/applications"
    mkdir -p "$BUILD_DIR/usr/share/icons/hicolor/256x256/apps"
    mkdir -p "$BUILD_DIR/usr/local/bin"

    # Copy app
    cp -r "$SCRIPT_DIR/app" "$BUILD_DIR/opt/${PKG_NAME}/"
    cp "$SCRIPT_DIR/run.sh" "$BUILD_DIR/opt/${PKG_NAME}/"

    # Icon (hicolor)
    if [ -f "$SCRIPT_DIR/app/assets/icon.png" ]; then
        cp "$SCRIPT_DIR/app/assets/icon.png" \
           "$BUILD_DIR/usr/share/icons/hicolor/256x256/apps/${PKG_NAME}.png"
    fi

    # Create venv — prefer python3.12 (has dev headers), fallback to system python3
    cd "$BUILD_DIR/opt/${PKG_NAME}/app"
    if command -v uv &> /dev/null; then
        # uv can find the right Python
        if python3.12 --version >/dev/null 2>&1; then
            uv venv .venv --python python3.12 --seed
        else
            uv venv .venv --python /usr/bin/python3 --seed
        fi
        source .venv/bin/activate
        uv pip install -r requirements.txt
        deactivate
    else
        PYTHON=$(command -v python3.12 || echo /usr/bin/python3)
        $PYTHON -m venv .venv
        .venv/bin/pip install -q -r requirements.txt
    fi

    # Launcher
    cat > "$BUILD_DIR/usr/local/bin/${PKG_NAME}" << 'LAUNCHER'
#!/bin/bash
cd /opt/ai-typer-v2/app
exec .venv/bin/python3 -m src.main "$@"
LAUNCHER
    chmod +x "$BUILD_DIR/usr/local/bin/${PKG_NAME}"

    # Desktop entry
    cat > "$BUILD_DIR/usr/share/applications/${PKG_NAME}.desktop" << EOF
[Desktop Entry]
Name=Voxtype
GenericName=Voice Typer
Comment=Voice dictation with multimodal AI cleanup
Exec=${PKG_NAME}
Icon=${PKG_NAME}
Terminal=false
Type=Application
Categories=Utility;Audio;
EOF

    # Control file
    cat > "$BUILD_DIR/DEBIAN/control" << EOF
Package: ${PKG_NAME}
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: amd64
Depends: python3, ffmpeg, portaudio19-dev
Maintainer: Daniel Rosehill <public@danielrosehill.com>
Description: Voice dictation with multimodal AI cleanup
 Speak naturally, get polished text. Uses multimodal AI
 to transcribe and clean up dictation in a single pass.
EOF

    # Build
    cd "$DIST_DIR"
    dpkg-deb --build "build/${PKG_NAME}_${VERSION}" "${PKG_NAME}_${VERSION}_amd64.deb"
    rm -rf "build/"

    echo "Built: $DIST_DIR/${PKG_NAME}_${VERSION}_amd64.deb"
}

case "${1:-}" in
    --deb)
        build_deb "$2"
        ;;
    --dev)
        build_deb
        echo "Installing..."
        sudo dpkg -i "$SCRIPT_DIR/dist/ai-typer-v2_$(get_version)_amd64.deb"
        ;;
    --help|"")
        show_help
        ;;
    *)
        echo "Unknown command: $1"
        show_help
        exit 1
        ;;
esac
