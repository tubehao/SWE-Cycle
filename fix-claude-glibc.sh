#!/bin/bash
for dir in ~/.vscode-server/extensions/anthropic.claude-code-*-linux-x64/resources/native-binary/; do
  bin="$dir/claude"
  [ -f "$bin" ] || continue
  if head -1 "$bin" 2>/dev/null | grep -q "^#!/bin/bash"; then
    echo "Already wrapped: $bin"
    continue
  fi
  cp "$bin" "$bin.real"
  cat > "$bin" << 'INNER'
#!/bin/bash
GLIBC_DIR="$HOME/glibc-new"
SELF_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
exec "$GLIBC_DIR/ld-linux-x86-64.so.2" --library-path "$GLIBC_DIR":/lib64:/usr/lib64 --argv0 "$SELF_DIR/claude" "$SELF_DIR/claude.real" "$@"
INNER
  chmod +x "$bin"
  echo "Wrapped: $bin"
done
