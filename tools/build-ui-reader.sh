#!/bin/sh
# 从可审计的 Java 源码重新生成只读 UI 采集器；运行脚本本身不需要安装 Java。
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SDK=${ANDROID_HOME:-"$HOME/Library/Android/sdk"}
PLATFORM=${ANDROID_PLATFORM:-android-37.0}
BUILD_TOOLS=${ANDROID_BUILD_TOOLS:-36.0.0}
BUILD=$(mktemp -d "${TMPDIR:-/tmp}/bbnc-reader.XXXXXX")
mkdir -p "$BUILD/classes" "$BUILD/dex"
"${JAVA_HOME:?请设置 JAVA_HOME}/bin/javac" -source 8 -target 8 \
  -cp "$SDK/platforms/$PLATFORM/android.jar" -d "$BUILD/classes" "$ROOT/tools/FarmDump.java"
"$SDK/build-tools/$BUILD_TOOLS/d8" --lib "$SDK/platforms/$PLATFORM/android.jar" \
  --output "$BUILD/dex" "$BUILD/classes/FarmDump.class"
cp "$BUILD/dex/classes.dex" "$ROOT/tools/farmdump.dex"
echo "已生成 $ROOT/tools/farmdump.dex；构建中间文件：$BUILD"
