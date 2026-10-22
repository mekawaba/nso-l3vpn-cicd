#!/usr/bin/env bash
# Compile an NSO package inside the NSO Build Image and bundle it as <name>-<version>.tar.gz.
# The tarball is then tested on the Production Image (NSO_IMAGE).
#
# Usage: ci/build_package.sh <package-dir> <work-dir> <output-dir>
# Prints the path of the created tarball on the last line.
set -euo pipefail

src=$(cd "$1" && pwd)
work=$2
out=$3
image=${NSO_BUILD_IMAGE:-cisco-nso-build:6.7.4}
name=$(basename "$src")
version=$(sed -n 's:.*<package-version>\(.*\)</package-version>.*:\1:p' "$src/package-meta-data.xml")

rm -rf "$work"
mkdir -p "$work" "$out"
out=$(cd "$out" && pwd)
cp -r "$src" "$work/$name"
rm -rf "$work/$name/load-dir"
find "$work/$name" -name __pycache__ -prune -exec rm -rf {} +

# ncsc is executable by the image's nso user only, so build as that user in a
# scratch copy that both the runner and the container can write.
chmod -R a+rwX "$work"
docker run --rm --network none -v "$work/$name:/pkg" --entrypoint sh "$image" \
  -c "umask 000 && make -C /pkg/src clean all"

tarball="$out/$name-$version.tar.gz"
tar -C "$work" -czf "$tarball" "$name"
echo "$tarball"
